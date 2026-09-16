"""人工审批通道 —— 护栏要"有人能说不"，得先有地方问。

★ 为什么这个模块必须自己造（ADR-4）：
  browser-use 0.13.10 全库零命中 HITL（人工确认）机制。而"危险操作要人点确认"
  是本项目护栏的核心主张之一 —— 所以它不是"用库的某个开关"，是从零写的。

★ 唯一的对外契约是 Approver 协议的一个方法：
      async def request(req: ApprovalRequest) -> ApprovalResult
  四种实现（Web / CLI / File / AutoDeny）都满足它，拦截器只认这个方法。
  换通道不改拦截器，也不改策略 —— 这是刻意的：审批通道是【基础设施】，
  策略是【业务判断】，两者的变化频率完全不同，不该耦合。

★ 所有通道一律 fail-closed（超时/异常/读不懂的回答 = 拒绝）。
  这一条不能靠每个实现自觉遵守，所以它写在 BaseApprover.request 里，
  子类只实现"怎么问"，没有机会忘记"答不上来怎么办"。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ecom_agent.config import APPROVAL_TIMEOUT_S, DECIDED_DIR, PENDING_DIR

logger = logging.getLogger(__name__)

# ★ 这个前缀是【契约】，不是文案。
#   Phase 5 的验收标准之一就是"steps.jsonl 里有 HUMAN_DENIED"——
#   报告、日志检索、测试断言都 grep 它。改文案可以，改前缀要同步改那些地方。
DENIED_ERROR_PREFIX = "HUMAN_DENIED"


class ApprovalOutcome(str, Enum):
    """审批的结局。

    ★ 为什么要把 denied / timeout / error 分成三种，而不是合成一个"没批准"：
      它们在事后报告里的含义完全不同。审计员明确点了"拒绝"，
      和"根本没人看到这条请求"，是两件性质不同的事 ——
      前者说明护栏按预期工作，后者说明审批通道本身有问题。
      合成一种会让后一种问题永远显示成前一种，从而永远不被发现。
    """

    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"
    ERROR = "error"

    @property
    def approved(self) -> bool:
        return self is ApprovalOutcome.APPROVED


class ApprovalRequest(BaseModel):
    """一条待审批的操作。

    ⚠️ 构造它之前必须先脱敏（observability/redact.py）。
       这份对象会被写进 pending/*.json、推到 Web 界面、落进 steps.jsonl ——
       approver 不再做第二遍脱敏，因为它无从知道哪些值是敏感的。
       脱敏点必须在使用点之前，而不是在某个下游实现里。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str = ""
    step: int = 0

    action_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    element_text: str | None = None
    url: str = ""

    rule_id: str | None = None
    reason: str = ""

    def summary(self) -> str:
        """给人看的一行摘要（CLI 提示、Web 卡片标题都用它）。"""
        what = self.element_text or self.action_name
        return f"[{self.rule_id or 'default'}] {self.action_name}「{what}」 @ {self.url}"


@dataclass(frozen=True)
class ApprovalResult:
    outcome: ApprovalOutcome
    approved_by: str = ""
    decided_at: str = ""
    note: str = ""

    @property
    def approved(self) -> bool:
        return self.outcome.approved

    def deny(self) -> bool:
        return not self.approved

    def to_action_error(self, req: ApprovalRequest) -> str:
        """转成 ActionResult.error 的文案 —— 这句话是【给 LLM 看的】。

        ★ 为什么必须把原因说清楚、而不是简单写 "denied"：
          被拦的动作不是被删掉，而是被换成一条说明（见计划里的设计）。
          如果只说"被拒绝"，LLM 不知道是"这个动作本身不行"还是"时机不对"，
          它最可能的反应是【换个说法再试一次同一个危险动作】。
          写清"这是人工拒绝/超时"，它才会去改道。
        """
        why = {
            ApprovalOutcome.DENIED: "人工审批被拒绝",
            ApprovalOutcome.TIMEOUT: "人工审批超时（无人应答，按拒绝处理）",
            ApprovalOutcome.ERROR: "审批通道故障（按拒绝处理）",
        }.get(self.outcome, "未获批准")
        who = f"，审批人={self.approved_by}" if self.approved_by else ""
        detail = f"；{self.note}" if self.note else ""
        return (
            f"{DENIED_ERROR_PREFIX}: 动作 {req.action_name}「{req.element_text or ''}」未获批准"
            f"（{why}{who}，规则={req.rule_id or 'default'}）。"
            f"不要重复这个动作，请换一种方式完成目标，或直接调用 done 汇报受阻原因{detail}"
        )


@runtime_checkable
class Approver(Protocol):
    """审批通道的唯一契约。拦截器只依赖它，不依赖任何具体实现。"""

    async def request(self, req: ApprovalRequest) -> ApprovalResult: ...


# ── 基类：把 fail-closed 焊死在一处 ────────────────────────
class BaseApprover:
    """所有实现继承它。子类只写"怎么问"，不写"答不上来怎么办"。

    ★ 这个划分是刻意的：如果把超时处理留给每个子类，
      那么"新加一个通道时忘了处理超时"就是一个必然会发生的事故，
      而且它的表现形式是【默认放行】—— 最坏的那种失败。
      放在基类里，子类就算什么都不做，最坏结果也是拒绝。
    """

    #: True 表示这个通道会无脑放行（只允许在本地调试用）。
    #: RunRecorder 会把它记进 run.json 的 unsafe_auto_approved 字段 ——
    #: 目的是让"调试后门"在被误用到真实场景时留下痕迹，而不是悄悄生效。
    unsafe: bool = False

    def __init__(self, *, timeout_s: float | None = APPROVAL_TIMEOUT_S, run_id: str = "") -> None:
        self.timeout_s = timeout_s
        self.run_id = run_id

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        if not req.run_id:
            req = req.model_copy(update={"run_id": self.run_id})
        try:
            return await asyncio.wait_for(self._decide(req), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            # asyncio.wait_for 超时会 cancel 内部协程；子类的清理写在 finally 里。
            await self._on_timeout(req)
            logger.warning("审批超时（%.0fs），按拒绝处理：%s", self.timeout_s or 0, req.summary())
            return ApprovalResult(ApprovalOutcome.TIMEOUT, note=f"超过 {self.timeout_s}s 无人应答")
        except asyncio.CancelledError:
            # ★ 必须原样抛出：这是 run 被外部取消，不是审批失败。
            #   吞掉它会让"停止 run"变成一个静默失败的按钮。
            raise
        except Exception as e:  # noqa: BLE001 —— 通道坏了也必须拒绝，不能放行
            logger.warning("审批通道异常，按拒绝处理：%s", req.summary(), exc_info=True)
            return ApprovalResult(ApprovalOutcome.ERROR, note=f"{type(e).__name__}: {e}")

    async def _decide(self, req: ApprovalRequest) -> ApprovalResult:
        raise NotImplementedError

    async def _on_timeout(self, req: ApprovalRequest) -> None:
        """超时清理钩子。默认什么都不做。"""

    async def aclose(self) -> None:
        """释放资源。默认什么都不做。"""


# ── Web：默认通道，等 Web 层 POST 决策来唤醒 ───────────────
class WebApprover(BaseApprover):
    """写 pending/{id}.json，然后 await 一个 asyncio.Event。

    ★ 为什么用 Event 而不是轮询文件：Web 层就在同一个进程里（FastAPI 与 run
      任务共享事件循环），所以"唤醒"可以是真的唤醒。轮询在这里只会平白
      增加延迟和磁盘 IO。
      而 FileApprover 必须轮询 —— 它的使用者是【另一个进程】，没有共享内存。

    ★ pending/ 里的文件在进程重启后会留下来。这不是没清理干净，
      而是一个有用的信号：pending/ 非空 = 上一个 run 在等审批时死了。
      所以 resolve() 是把它【移走】而不是删掉，decided/ 成为一份审批流水。
    """

    def __init__(
        self,
        *,
        pending_dir: Path | None = None,
        decided_dir: Path | None = None,
        timeout_s: float | None = APPROVAL_TIMEOUT_S,
        run_id: str = "",
    ) -> None:
        super().__init__(timeout_s=timeout_s, run_id=run_id)
        self.pending_dir = Path(pending_dir or PENDING_DIR)
        self.decided_dir = Path(decided_dir or DECIDED_DIR)
        self._events: dict[str, asyncio.Event] = {}
        self._results: dict[str, ApprovalResult] = {}

    async def _decide(self, req: ApprovalRequest) -> ApprovalResult:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.decided_dir.mkdir(parents=True, exist_ok=True)

        event = asyncio.Event()
        self._events[req.id] = event
        _write_json(self.pending_dir / f"{req.id}.json", req.model_dump(mode="json"))
        logger.info("等待人工审批：%s（id=%s）", req.summary(), req.id)

        try:
            await event.wait()
        finally:
            # ★ 必须在 finally 里清理：超时时 wait_for 会 cancel 这里，
            #   不清理的话 _events 会随着 run 增长一路泄漏，
            #   而且被取消的那条请求会永远留在 pending/ 里冒充"还在等"。
            self._events.pop(req.id, None)

        return self._results.pop(req.id, ApprovalResult(ApprovalOutcome.DENIED, note="无决策"))

    def resolve(
        self,
        approval_id: str,
        approved: bool,
        *,
        approved_by: str = "web",
        note: str = "",
    ) -> bool:
        """Web 层决策入口。返回 False 表示这条审批已经不在等待中（重复提交／已超时）。"""
        event = self._events.get(approval_id)
        if event is None:
            logger.warning("审批 %s 不在等待中（已超时或已决策），忽略本次提交", approval_id)
            return False

        result = ApprovalResult(
            ApprovalOutcome.APPROVED if approved else ApprovalOutcome.DENIED,
            approved_by=approved_by,
            decided_at=_now(),
            note=note,
        )
        self._results[approval_id] = result
        # ★ 先移走 pending 文件再唤醒：反过来的话，Web 层可能在文件还在 pending/
        #   的瞬间被唤醒并拉到一条"仍在等待"的列表，显示出一条已经决策的卡片。
        pending = self.pending_dir / f"{approval_id}.json"
        if pending.is_file():
            payload = json.loads(pending.read_text(encoding="utf-8"))
            # ★ 手写 dict 而不用 dataclasses.asdict：asdict 会把 outcome 这个
            #   Enum 原样留着，最后靠 json 的 default=str 兜成
            #   "ApprovalOutcome.DENIED" —— 一个能读但无法比较的字符串。
            #   流水文件是要被下游程序解析的，写 "denied" 才是它该有的形态。
            payload["result"] = {
                "outcome": result.outcome.value,
                "approved": result.approved,
                "approved_by": result.approved_by,
                "decided_at": result.decided_at,
                "note": result.note,
            }
            # ★ 先写 decided/（含原请求 + 决策），再删 pending/：
            #   顺序反了的话，进程在这两步之间死掉就会【两条记录都没有】——
            #   一次审批从此无迹可寻。
            _write_json(self.decided_dir / f"{approval_id}.json", payload)
            pending.unlink(missing_ok=True)

        event.set()
        return True

    def waiting(self) -> list[str]:
        """当前等待中的审批 id（Web 层用来渲染待办卡片）。"""
        return sorted(self._events)

    async def _on_timeout(self, req: ApprovalRequest) -> None:
        (self.pending_dir / f"{req.id}.json").unlink(missing_ok=True)


# ── CLI：本机调试，人就在终端前面 ──────────────────────────
class CliApprover(BaseApprover):
    """在终端里问一句 y/N。

    ★ input() 必须走 asyncio.to_thread。
      直接调用会阻塞事件循环 —— 后果不是"卡一下"，而是 CDP 心跳发不出去、
      浏览器连接被判超时。表现为"审批完之后浏览器就坏了"，
      而根因是一个看起来完全正常的 input()。

    ⚠️ 已知限制（老实写出来，不假装能解决）：
      被 wait_for 取消时，to_thread 里那个线程【不会停】—— 它仍然阻塞在 stdin 上。
      于是超时之后，用户按下的那一行会被【下一次】审批的 input() 读走。
      这个坑没法在纯 asyncio 层面修（要修得自己起线程 + 队列，而队列又会
      引入"超时后到达的回答算哪一次"的歧义）。所以：
      无人值守场景请用 WebApprover / FileApprover，别用这个。
    """

    def __init__(self, *, timeout_s: float | None = APPROVAL_TIMEOUT_S, run_id: str = "") -> None:
        super().__init__(timeout_s=timeout_s, run_id=run_id)

    async def _decide(self, req: ApprovalRequest) -> ApprovalResult:
        tail = f"（{self.timeout_s:.0f} 秒后自动拒绝）" if self.timeout_s else ""
        prompt = (
            f"\n⚠️  需要人工确认\n"
            f"    规则: {req.rule_id or 'default'}\n"
            f"    原因: {req.reason}\n"
            f"    动作: {req.action_name}\n"
            f"    元素: {req.element_text or '（无可读文本）'}\n"
            f"    URL : {req.url}\n"
            f"    批准执行？[y/N] {tail} "
        )
        try:
            answer = await asyncio.to_thread(input, prompt)
        except EOFError:
            # ★ stdin 被关掉（CI、被重定向、非交互环境）时 input() 抛的就是这个。
            #   不接的话它会一路冒到 request() 的 except Exception → ERROR，
            #   语义上不算错，但"stdin 不可用"其实是【必然的配置问题】，
            #   值得单独说清楚 —— 否则报错会显示成"审批通道故障"，
            #   而真正该做的是"换个通道"。
            logger.warning("stdin 不可用（非交互环境？），无法用 CLI 审批 —— 建议改用 FileApprover")
            return ApprovalResult(ApprovalOutcome.ERROR, note="stdin 不可用")

        # ★ 默认拒绝：回车、乱输入、大小写混写都走这里。
        #   宁可让一次正常操作被拒（再点一次就好），也不能让一次危险操作被放行。
        ok = answer.strip().lower() in {"y", "yes", "是", "批准", "同意"}
        return ApprovalResult(
            ApprovalOutcome.APPROVED if ok else ApprovalOutcome.DENIED,
            approved_by="cli",
            decided_at=_now(),
            note=f"终端回答={answer.strip()!r}",
        )


# ── File：无头 / CI，跨进程通信 ────────────────────────────
class FileApprover(BaseApprover):
    """写 pending/{id}.json，轮询 decided/{id}.json。

    ★ 为什么必须轮询：它的使用者是【另一个进程】（CI 脚本、一个审批小工具、
      或者就是"人手工 touch 一个文件"）。跨进程没有共享内存，
      事件循环里的 Event 对另一个进程毫无意义。
      轮询间隔取 1s：对"人来做决定"这个时间尺度，1s 的延迟完全无感，
      而更短的间隔只是在空转烧 CPU。
    """

    def __init__(
        self,
        *,
        pending_dir: Path | None = None,
        decided_dir: Path | None = None,
        timeout_s: float | None = APPROVAL_TIMEOUT_S,
        poll_interval_s: float = 1.0,
        run_id: str = "",
    ) -> None:
        super().__init__(timeout_s=timeout_s, run_id=run_id)
        self.pending_dir = Path(pending_dir or PENDING_DIR)
        self.decided_dir = Path(decided_dir or DECIDED_DIR)
        self.poll_interval_s = poll_interval_s

    async def _decide(self, req: ApprovalRequest) -> ApprovalResult:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.decided_dir.mkdir(parents=True, exist_ok=True)
        target = self.decided_dir / f"{req.id}.json"
        _write_json(self.pending_dir / f"{req.id}.json", req.model_dump(mode="json"))
        logger.info("等待人工审批（文件通道）：%s（id=%s）", req.summary(), req.id)

        try:
            while not target.is_file():
                await asyncio.sleep(self.poll_interval_s)
        finally:
            (self.pending_dir / f"{req.id}.json").unlink(missing_ok=True)

        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            approved = bool(payload["approved"])
        except Exception:  # noqa: BLE001 —— 读不懂的决策文件一律当拒绝
            # ★ 一个写坏了/写了一半的决策文件，绝不能因为"解析不了"就被当成放行。
            logger.warning("决策文件无法解析，按拒绝处理：%s", target)
            return ApprovalResult(ApprovalOutcome.ERROR, note=f"决策文件无法解析：{target.name}")

        return ApprovalResult(
            ApprovalOutcome.APPROVED if approved else ApprovalOutcome.DENIED,
            approved_by=str(payload.get("approved_by", "file")),
            decided_at=str(payload.get("decided_at", _now())),
            note=str(payload.get("note", "")),
        )


# ── 永远拒绝：测"被拒之后 LLM 能不能改道"的唯一办法 ────────
class AutoDenyApprover(BaseApprover):
    """自动拒绝。★ 别把它当"测试用的偷懒实现"——它测的是最容易漏的一条分支。

    护栏的失败路径（拒绝 → LLM 改道）是这套系统里最容易被漏测的：
    手工测试时人总是点"批准"，因为你在验证"任务能不能跑完"。
    于是"拒绝之后 run 到底是继续还是崩掉""LLM 是改道还是死循环"
    这类问题会一直潜伏到真实使用。这个通道让那条路径变成一条常规单测。
    """

    timeout_s = None  # 立刻有结论，不涉及等待

    def __init__(self, *, note: str = "AutoDenyApprover", run_id: str = "") -> None:
        super().__init__(timeout_s=None, run_id=run_id)
        self._note = note

    async def _decide(self, req: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult(
            ApprovalOutcome.DENIED, approved_by="auto-deny", decided_at=_now(), note=self._note
        )


# ── 永远批准：调试后门，会自报家门 ─────────────────────────
class AutoApproveApprover(BaseApprover):
    """自动批准。⚠️ 每次调用都会打 WARNING，且 unsafe=True。

    ★ 为什么要打日志、要标 unsafe，而不是安静地用：
      "调试时为了跑通临时改成自动批准"是一个极其自然的动作，
      而它最容易的后果是【忘了改回去】，然后这个后门就成了生产配置。
      留下可检索的痕迹（WARNING + run.json 的 unsafe_auto_approved）
      不能阻止这件事发生，但能让它在事后被查出来 ——
      而且面试时"我知道这个后门会怎么变成事故，所以我给它装了记录"是个真实答案。
    """

    unsafe = True

    def __init__(self, *, timeout_s: float | None = None, run_id: str = "") -> None:
        super().__init__(timeout_s=timeout_s, run_id=run_id)

    async def _decide(self, req: ApprovalRequest) -> ApprovalResult:
        warnings.warn(
            f"AutoApproveApprover 自动批准了 {req.summary()} —— 仅限本地调试，不要用于真实账号",
            UserWarning,
            stacklevel=2,
        )
        logger.warning("⚠️ 自动批准（不安全的调试通道）：%s", req.summary())
        return ApprovalResult(
            ApprovalOutcome.APPROVED,
            approved_by="auto-approve(UNSAFE)",
            decided_at=_now(),
            note="调试通道自动批准",
        )


# ── 工厂 ──────────────────────────────────────────────────
def get_approver(kind: str, **kwargs: Any) -> BaseApprover:
    """按名字造通道。名字来自配置（ECOM_AGENT_APPROVER），默认 web。

    ★ 用工厂而不是让调用方 if/else：将来加第五个通道（比如钉钉、企业微信）
      只需要在这里加一行，而拦截器、runner、Web 层一行都不用改。
    """
    table: dict[str, type[BaseApprover]] = {
        "web": WebApprover,
        "cli": CliApprover,
        "file": FileApprover,
        "deny": AutoDenyApprover,
        "auto-approve": AutoApproveApprover,
    }
    key = (kind or "web").strip().lower()
    if key not in table:
        raise ValueError(f"未知的审批通道 {kind!r}；可选：{sorted(table)}")
    return table[key](**kwargs)


# ── 小工具 ────────────────────────────────────────────────
def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """★ 显式 encoding="utf-8"：审批内容里有中文（元素文本、规则原因）。
      不写的话在 Windows 上按 GBK 写，读回来时是乱码 —— 而乱码的元素文本
      直接让 match_element_text 的审计信息失去意义（人看不懂当时点的是什么）。

    ★ 先写临时文件再 os.replace 做原子替换：读方（Web 界面、另一个进程）
      可能正好在写的中间来读，读到半个 JSON 会解析失败。
      replace 是原子的，读方要么看到旧文件、要么看到完整的新文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    tmp.replace(path)
