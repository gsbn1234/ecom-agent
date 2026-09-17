"""TaskRunner —— 把一份 CompiledTask 跑成一套可回放的产物。

产物（`runs/{run_id}/`）：run.json / steps.jsonl / screenshots/ / result.json / report.html

★★ 本文件里有两件"必须做对否则静默失效"的事，各占一段长注释：

  1. **`on_step_end` 的"history 有没有变长"闸门**（`_record_step`）——
     库会在没有产生新历史项的情况下照样调用这个钩子，
     而那时 `history.history[-1]` 指的是**上一步**，直接记就是把上一步记两遍。
     两遍的记录看起来完全正常，只是盘上多了一行、而少了一行。

  2. **LLM 调用次数的"先观测、后解释"**（`interpret_llm_usage`）——
     总数是数出来的（权威），分项是解释出来的（可能错）。
     两者一旦合并成"分项之和等于总数"，解释错的时候就再也看不出来了。

★ 另外，本文件是**唯一**做下面这些决定的地方（它们都不属于纯逻辑层）：
  · start_url 必须在白名单里 —— 且必须在创建浏览器之前判
  · 什么算 COMPLETED / FAILED / BLOCKED —— 三种结局的处置方式完全不同
  · 什么情况下重试、什么情况下不重试
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from ecom_agent import compat
from ecom_agent.actions import build_tools, verify_extract_table_round_trip
from ecom_agent.config import DB_PATH, LIVE_LLM, RUNS_DIR
from ecom_agent.dsl.compiler import CompiledTask
from ecom_agent.guardrails.interceptor import GuardrailInterceptor
from ecom_agent.guardrails.policy import GuardrailPolicy
from ecom_agent.observability.models import (
    BLOCKED,
    COMPLETED,
    FAILED,
    LlmUsage,
    RunRecord,
    StepRecord,
    now_iso,
)
from ecom_agent.observability.recorder import RunRecorder, new_run_id
from ecom_agent.observability.redact import Redactor
from ecom_agent.observability.report import render_report, write_report
from ecom_agent.sites.pinduoduo.output_models import ParseStatus

logger = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    """开跑**之前**就能判定的配置问题（缺 key、start_url 不在白名单、通道名写错）。

    ★★ 为什么值得为此定义一个专用异常，而不是随手 raise RuntimeError：

      CLI 需要把"配置错了"和"跑挂了"分到不同的退出码上，而两者都会往上冒。
      用裸 RuntimeError 的话，CLI 只能 `except RuntimeError` —— 于是**库内部**
      任何一处 RuntimeError（browser_use 里不少）都会被误报成"你的配置有问题"，
      而真正的现场信息（栈）已经被这条 except 吃掉了。
      排查的人会去改配置，而问题在别处。

      ★ 同理它继承 RuntimeError 而不是 Exception：万一某处没接住，
        "运行期错误"这个粗分类仍然是对的，不会变成"未知异常"。
    """


# ── 结果 ──────────────────────────────────────────────────
@dataclass
class RunOutcome:
    """一次 run 的结局。★ CLI / Web 层只读这个对象，不各自去猜状态。"""

    run_id: str
    run_dir: Path
    status: str
    parse_status: str
    rows_collected: int
    record: RunRecord
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """★ `status == COMPLETED` **不等于**"采到了数据"。
        「跑完了但一行没采到」是合法结局（parse_status=empty），
        把它算成 ok 会让调用方以为有数据；算成不 ok 又会让
        on_empty_result=accept 这个明确的选择看起来没生效。
        所以两个问题分开回答：ok 回答"这次运行本身成不成功"，
        rows_collected 回答"采到了几行"。"""
        return self.status == COMPLETED

    def summary(self) -> str:
        return (
            f"run {self.run_id} → {self.status} / {self.parse_status}"
            f" / {self.rows_collected} 行 / 产物在 {self.run_dir}"
        )


# ── 纯函数：状态判定与解释（可离线单测，零浏览器零 token）──
def check_start_url(compiled: CompiledTask) -> str | None:
    """起点 URL 是否在白名单内。返回错误说明，或 None 表示通过。

    ★★ 为什么这是一条**独立于浏览器**的检查：
      `start_url` 是 DSL 里的一个字段，但在这个函数存在之前它**没有任何强制点** ——
      编译器渲染 task_text 时根本不包含它（task_text 是 goal + 步骤 + 参数 +
      分页 + 护栏条款），所以它既不进提示词、也没有人拿它去导航。
      表现是：agent 从 about:blank 起步，然后"自己想办法"打开页面 ——
      要么失败，要么打开一个它自己猜的 URL（而那个 URL 会走 Layer 0，
      于是被白名单拦下，报一个看起来像网络问题的错）。

      一个声明了却没人执行的字段，比没有这个字段更糟：它让人以为被管住了。

    ★ 判在创建浏览器之前：非法配置不该烧 token，也不该启动一个浏览器进程。
    """
    url = compiled.spec.start_url
    if not url:
        return "任务没有配置 start_url —— 不知道从哪里开始"
    verdict = compiled.policy.check_navigation(url)
    if not verdict.is_allowed:
        return (
            f"start_url 不在白名单内：{url}（判定：{verdict.decision.value}，"
            f"规则={verdict.rule_id or 'default'}，{verdict.reason}）。"
            f"白名单是 {compiled.spec.guardrails.allowed_domains} —— "
            f"这是配置错误，不是运行时问题；LIVE 模式下这条一定会被 Layer 0 拦下"
        )
    return None


def unrunnable_rule_actions(compiled: CompiledTask, tools: Any) -> list[tuple[str, str]]:
    """找出规则里引用了**当前未注册动作**的条目，返回 `[(rule_id, action_name)]`。

    ★★ 为什么值得在启动时查一遍：
      `match_action` 是"动作名必须精确相等"，所以一条规则里的动作名一旦拼错
      （或者写的是一个**还没实现**的自定义 action），那条规则就**永远不会命中**。
      失败形态是彻头彻尾的静默：规则在 YAML 里躺着、编译进 task_text、
      进了护栏条款、报告里也显示策略已加载 —— 而它一次都没生效过。
      这和本项目其他几处（selector_map 被清空、guard_notice 注入失败）
      属于同一类：**看起来在防，实际没防。**

    ★ 只警告不报错：自定义 action 是分阶段加的（比如 `extract_table`
      在 Phase 4 才注册）。一个"将来会存在"的动作名不该让今天的 run 起不来。
      但**必须打出来** —— 否则"这条规则是摆设"这件事只有把两个文件对着读才发现。
    """
    registry = getattr(getattr(tools, "registry", None), "registry", None)
    registered = set(getattr(registry, "actions", None) or {})
    if not registered:
        # 拿不到注册表就不表态。★ 不返回"全部规则都不可用"——
        # 那是一个**误报**，而假警报会训练人忽略这个检查。
        return []

    out: list[tuple[str, str]] = []
    for rule in compiled.spec.guardrails.rules:
        for name in rule.match_action or ():
            if name not in registered:
                out.append((rule.id, name))
    return out


def unreachable_text_rules(
    compiled: CompiledTask, tools: Any
) -> list[tuple[str, str]]:
    """找出「写了 `match_element_text`，却把不可能带元素文本的动作也列进 `match_action`」
    的条目，返回 `[(rule_id, action_name)]`。

    ★★ 这是 `unrunnable_rule_actions` 的**另一半**，两者合起来才覆盖
      "规则永远不会命中"的全部成因：

        · `unrunnable_rule_actions`：动作名**根本不存在**（拼错 / 还没实现）
        · 本函数：动作名存在，但**这个维度对它不适用**

      第二条更隐蔽：YAML 读起来完全正常，动作名也是真的，
      review 的人没有任何理由怀疑它。它的成因是两个都正确的决定相乘：

        1. `match_element_text` 拿不到文本时判【不命中】（rules.py:117-124，
           刻意如此 —— 把 None 当空串会让写错的规则拦死一切）
        2. `_text_for` 只从参数里的 `index` 取值（interceptor.py:517-526）

      → 任何"不针对元素"的动作（`extract` / `go_back` / `extract_table` / `navigate` …）
        在这些规则里**永远不可能命中**。

    ★ 实测到的后果不是"少拦了危险动作"（那方向反而安全），而是
      **模板声称的意图 ≠ 实际策略**：三份模板里都写着
      "只读检索类操作 → allow"，而 `extract` 实际落到了 `default_decision=confirm`。
      一个说 allow 实际是 confirm 的规则，比没写这条规则更坏：
      读 YAML 的人会以为只读动作已经放行了。

    ★ 只警告不报错（同 `unrunnable_rule_actions`）：方向是 fail-closed，
      不该让 run 起不来。但必须打出来 —— 否则只有把两个文件对着读才发现。
    """
    bearing = compat.index_bearing_actions(tools)
    if bearing is None:
        return []
    out: list[tuple[str, str]] = []
    for rule in compiled.spec.guardrails.rules:
        if not rule.match_element_text:
            # ★ 没写 match_element_text 的规则不受影响 —— 它不靠元素文本判定，
            #   在 extract 这类动作上是**能**命中的（mock 模板就靠这个正确处理了
            #   extract_table）。这个分支是"不要误报"的关键。
            continue
        for name in rule.match_action or ():
            if name not in bearing:
                out.append((rule.id, name))
    return out


def classify_parse_status(
    *, structured: Any, rows: int, blocked: bool, error: str = ""
) -> str:
    """把"这次出了什么事"归到四档里的一档。**纯函数**。

    ★ 四档的意义在于**处置方式不同**，不是给报告凑字段：
        ok             → 入库
        empty          → 合法但零行；入 runs 表，products 零行（要人去区分
                         "确实没结果"和"选择器没找对"）
        schema_invalid → quarantine：整体拒绝入库，raw 原文留着
        blocked        → 不重试（重试只会再撞一次护栏，纯烧 token）
    """
    if blocked:
        return ParseStatus.BLOCKED.value
    if error:
        return ParseStatus.SCHEMA_INVALID.value
    if structured is None:
        # 没有结构化输出 = LLM 没调 done，或调了但没带出可解析的内容。
        # ★ 归到 schema_invalid 而不是 empty：empty 的含义是"查询确实没结果"，
        #   而这里是"我们根本没拿到一份符合 schema 的东西"—— 两回事。
        return ParseStatus.SCHEMA_INVALID.value
    if rows <= 0:
        return ParseStatus.EMPTY.value
    return ParseStatus.OK.value


def should_retry(compiled: CompiledTask, parse_status: str) -> bool:
    """这一档要不要再来一次。

    ★ BLOCKED 明确不重试（计划里的决定，理由：会再撞一次护栏）。
    ★ 重试**必须配一个新浏览器会话**：沿用旧会话意味着起点是一个
      上一次跑到一半的页面（可能停在一个弹窗、一个错误页、或某个详情页），
      而重试的语义是"从头再来一次"。复用会话会让第二次尝试的成功率
      取决于第一次失败时恰好停在哪 —— 一个不可复现的变量。
    """
    if parse_status == ParseStatus.BLOCKED.value:
        return False
    if parse_status == ParseStatus.OK.value:
        return False
    if parse_status == ParseStatus.EMPTY.value:
        return compiled.spec.retry.on_empty_result == "retry"
    return True  # schema_invalid / 其他异常 → 值得重来


def interpret_llm_usage(
    usage: LlmUsage, *, agent: Any, steps: Iterable[StepRecord]
) -> LlmUsage:
    """run 结束后，把"观测到的总调用"解释成四个分项。**就地改 usage 并返回它**。

    ★★ 为什么总分结构是这样的（这是本函数唯一的重点）：

      由 CountingLLM 直接数出来的**总数是权威**，由本函数解释出来的**分项可能错**。
      所以两者**分开存**，`LlmUsage.unexplained()` 把差额显式算出来。
      如果让 total_calls 变成一个 `step+judge+extract+other` 的 property，
      那么"解释错了"会表现为"总数也错了" —— 两个数一起漂移，
      没有任何一处对不上，而"我们对该库的调用模型已经过期"这件事
      就永远不会被人发现。

    ★ 分项怎么来的（三类，来源各不同）：
        step / judge —— 按**库自己用的输出类型名**去 by_format 里查。
                        名字是运行时问 agent 要的（`compat.llm_call_kinds`），
                        不是写死的常量 —— 写了常量，库一改名分类就静默全错。
        extract      —— 数 steps.jsonl 里的 `extract` 动作。
                        这是**间接**推算：一个 extract 动作通常对应一次
                        page_extraction_llm 调用，但动作失败时调用没发生。
        other        —— 残差。历史压缩、我们自己没识别出来的新调用都在这里。

    ★ `library_calls` 是独立对照（库自己的令牌账本长度）。它**允许**不等于总数：
      库只在 `result.usage` 为真时才记一条，所以失败的调用它一条都不记。
      两个数都存下来，让"哪一类调用不带 usage"变成一个可查的事实。
    """
    step_type, judge_type = compat.llm_call_kinds(agent)
    usage.step_calls = int(usage.by_format.get(step_type, 0)) if step_type else 0
    usage.judge_calls = int(usage.by_format.get(judge_type, 0))

    usage.extract_calls = sum(
        1 for s in steps for a in s.actions if a.name == "extract"
    )

    residual = usage.total_calls - usage.step_calls - usage.judge_calls - usage.extract_calls
    # ★ 残差可能是负的（extract 动作记了但请求没发出去）。
    #   这里 clamp 到 0 只为了让 other_calls 是"次数"而不是"负的次数"，
    #   差额本身不会因此消失 —— unexplained() 仍然是负的，summary() 会说出来。
    usage.other_calls = max(0, residual)

    usage.library_calls = compat.observed_llm_calls(agent)

    totals = compat.observed_token_totals(agent)
    if totals is not None and usage.prompt_tokens == 0 and usage.completion_tokens == 0:
        # ★ 只在"我们一个 token 都没数到"时才用库的账本兜底。
        #   两个来源都累加会**翻倍**，而翻倍看起来像"这个任务确实很贵"。
        usage.prompt_tokens, usage.completion_tokens = totals
    return usage


# ── 编排 ──────────────────────────────────────────────────
class TaskRunner:
    """一次 run 的编排者。一个 run 一个实例。

    ★ 为什么是类而不是一个长函数：run 级重试让"一次运行"天然分成多个 attempt，
      而 attempts 共享一批东西（run_id、run_dir、记录器、token 账、错误列表）。
      用闭包或全局变量传这批东西，会让"重试时哪些该重置、哪些该累积"
      变成一个靠读代码猜的问题 —— 那正是最容易出错的地方。
    """

    def __init__(
        self,
        compiled: CompiledTask,
        *,
        approver: Any | None = None,
        runs_dir: Path | str | None = None,
        run_id: str | None = None,
        llm: Any | None = None,
    ) -> None:
        self.compiled = compiled
        self.spec = compiled.spec
        self.approver = approver
        self.run_id = run_id or new_run_id()
        self.run_dir = Path(runs_dir or RUNS_DIR) / self.run_id

        # ★ 脱敏词表来自任务模板的 `observability.redact_extra`（如店铺名）。
        #   它在 DSL 里是一个**任务级**字段，因为"哪些词算敏感"是业务知识，
        #   只有写任务的人才说得清；内置规则（手机号/邮箱/身份证/凭证）另有一套。
        self.redactor = Redactor(literals=self.spec.observability.redact_extra)

        # ★ LLM 在这里只是**存着**，不在 __init__ 里构造：
        #   构造真 LLM 需要 key，而"没有 key"应该是一个能在**创建 run 目录之前**
        #   报出来的错误。__init__ 里构造会让"配错了 key"表现为
        #   "runs/ 下多了一个空目录"。
        self._llm_injected = llm
        self.usage = LlmUsage()

        self.errors: list[str] = []
        self.step_records: list[StepRecord] = []
        self.attempts = 0
        self._history_len = 0
        """`on_step_end` 的增长闸门用的游标。见 `_record_step` 的长注释。"""

        self._last_agent: Any = None
        """最后一个 attempt 的 Agent。★ 它的用途只有一处：run 结束后
        把 LLM 账解释清楚（`interpret_llm_usage` 要从它身上读**类型名**和
        库自己的令牌账本）。那两样都不依赖浏览器存活，所以能在会话关掉之后读。"""
        self._started_at = now_iso()
        self.recorder: RunRecorder | None = None

    # ── LLM ───────────────────────────────────────────────
    def _resolve_llm(self) -> Any:
        """注入优先，否则构造真的。★ 计数包装在这一步统一加上。"""
        from ecom_agent.runtime.llm import build_llm, counted

        if self._llm_injected is not None:
            # ★ 注入的（FakeLLM / 测试桩）也照样包一层计数 ——
            #   两条路径走同一套记账，否则"离线跑出来的数字"和
            #   "真跑出来的数字"含义不同，而对账时看不出这个区别。
            return counted(self._llm_injected, usage=self.usage)
        if not LIVE_LLM:
            raise PreflightError(
                "拒绝真调 LLM：需要同时满足 "
                "ECOM_AGENT_ENABLE_LIVE_LLM=true 且 DEEPSEEK_API_KEY 非空。"
                "（离线测试请显式注入 llm=；这个双条件开关的存在是为了让"
                " CI 与单测在没有任何 key 的情况下也能跑完全部用例。）"
            )
        return counted(build_llm(), usage=self.usage)

    # ── 主入口 ────────────────────────────────────────────
    async def run(self) -> RunOutcome:
        started = time.time()

        # ── 预检：全部在创建浏览器 / 烧 token 之前 ─────────
        problem = check_start_url(self.compiled)
        if problem:
            raise PreflightError(f"任务配置有问题，未启动浏览器：{problem}")

        llm = self._resolve_llm()
        tools = build_tools()

        # ★ 启动自检：**真的按库的方式调一次** extract_table，接线不通就当场死。
        #   放在这里而不是测试里，理由与 interceptor 的停机回调自检相同 ——
        #   这类接线错误的报错【指不到原因】，症状是"LLM 调了但没反应"。
        #   宁可 run 起不来，也不要一个"动作看起来注册了、实际调不动"的 run。
        #   （不需要浏览器：门禁用桩会话调，见 extract_table.py 的说明。）
        #   ⚠️ 必须 await：这个门禁内部要真的 await 一次动作函数，而
        #      `run()` 本身就是协程 —— 早先写成同步调用（内部 asyncio.run）
        #      时，它在【唯一的生产路径上】一次都没跑成过。细节见
        #      extract_table.py 里 verify_extract_table_round_trip 的说明。
        await verify_extract_table_round_trip(tools)

        for rule_id, action_name in unrunnable_rule_actions(self.compiled, tools):
            logger.warning(
                "护栏规则 %s 引用了未注册的动作 %r —— 这条规则永远不会命中。"
                "（拼错了？还是那个自定义 action 还没实现？）",
                rule_id,
                action_name,
            )

        # ★ 启动自检的另一半：动作名存在、但**这个维度对它不适用**。
        #   比上一条隐蔽得多 —— YAML 读起来完全正常，动作名也是真的。
        #   后果是"模板声称的意图 ≠ 实际策略"（比如写着 allow 实际是 confirm）。
        for rule_id, action_name in unreachable_text_rules(self.compiled, tools):
            logger.warning(
                "护栏规则 %s 既写了 match_element_text、又把动作 %r 列进了 match_action，"
                "但 %r 不针对任何元素（参数里没有 index）—— 这一条对它永远不会命中，"
                "实际处置会落到 default_decision=%s。"
                "（要放行这类只读动作，得像 tasks/mock_shop_readonly.yaml 那样"
                "单开一条不带 match_element_text 的规则）",
                rule_id,
                action_name,
                action_name,
                self.compiled.spec.guardrails.default_decision.value,
            )

        # ★ 记录器是**整个 run 一个**，不是每个 attempt 一个。
        #   理由：它是 run 级健康度计数（覆盖了几次、几步没快照、几张图）的持有者。
        #   每个 attempt 换一个的话，这些数字只会剩下最后一次 attempt 的 ——
        #   而"第一次尝试时观测是不是坏的"恰恰是重试场景下最该看的信息。
        #   attempt 之间用 `begin_attempt()` 切换，步号与截图路径都跟着分开。
        recorder = RunRecorder(
            run_id=self.run_id,
            task_id=self.compiled.task_id,
            run_dir=self.run_dir,
            redactor=self.redactor,
            screenshot=self.spec.observability.screenshot,
            record_llm_io=self.spec.observability.record_llm_io,
            attempt=1,
        )
        self.recorder = recorder

        max_attempts = max(1, self.spec.retry.max_attempts)
        parse_status = ParseStatus.SCHEMA_INVALID.value
        structured: Any = None
        history: Any = None
        blocked = False
        stop_reason = ""

        for attempt in range(1, max_attempts + 1):
            self.attempts = attempt
            recorder.begin_attempt(attempt)
            logger.info("── 第 %d/%d 次尝试 ──", attempt, max_attempts)

            try:
                attempt_out = await self._run_once(llm=llm, tools=tools, recorder=recorder,
                                                   attempt=attempt)
            except Exception as exc:  # noqa: BLE001 —— 一次 attempt 崩了要能落到产物里
                logger.exception("第 %d 次尝试异常终止", attempt)
                self.errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                attempt_out = _AttemptResult(
                    history=None, structured=None, product_rows=0, blocked=False,
                    stop_reason="", exception=exc,
                )

            history = attempt_out.history if attempt_out.history is not None else history
            structured = attempt_out.structured
            blocked = attempt_out.blocked
            stop_reason = attempt_out.stop_reason
            parse_status = classify_parse_status(
                structured=structured,
                rows=attempt_out.product_rows,
                blocked=blocked,
                error="" if attempt_out.exception is None else str(attempt_out.exception),
            )

            # 一次 attempt 里可能采到 0 行但确实是"查询没结果"，
            # 也可能因为异常压根没跑完 —— 后者 classify 已经归到 schema_invalid。
            logger.info(
                "第 %d 次尝试结束：parse_status=%s，采到 %d 行",
                attempt, parse_status, attempt_out.product_rows,
            )

            if not should_retry(self.compiled, parse_status):
                break
            if attempt == max_attempts:
                logger.warning("已用尽 %d 次尝试，最后一次仍是 %s", max_attempts, parse_status)
                break

        # ── 收尾：状态、账、产物 ──────────────────────────
        if blocked:
            status = BLOCKED
        elif parse_status == ParseStatus.EMPTY.value and self.spec.retry.on_empty_result == "fail":
            # ★ on_empty_result=fail 的语义就是"零行算失败"。不实现它的话，
            #   这个字段和"没写"没区别 —— 又是一个声明了却没人执行的配置。
            status = FAILED
            self.errors.append("任务声明 on_empty_result=fail，但一行都没采到")
        elif parse_status in (ParseStatus.OK.value, ParseStatus.EMPTY.value):
            status = COMPLETED
        else:
            status = FAILED

        if history is not None and self._last_agent is not None:
            # ★ 解释 LLM 账必须在 run() 之后做，但**不必**在会话关闭之前：
            #   `llm_call_kinds` 读的是 agent 上的**类型**，`observed_llm_calls`
            #   读的是库自己的令牌账本 —— 两者都不依赖浏览器活着。
            interpret_llm_usage(self.usage, agent=self._last_agent, steps=self.step_records)

        record = self._build_record(
            status=status,
            parse_status=parse_status,
            structured=structured,
            history=history,
            stop_reason=stop_reason,
            started=started,
            recorder=recorder,
        )

        # ★ 顺序是刻意的：**先入库，再写产物**。
        #   因为入库失败会被追进 self.errors，而 run.json 里也应该带上那条错误 ——
        #   否则会出现"库里写着落库失败、run.json 里一片干净"的分叉，
        #   而看报告的人只会看报告。反过来的顺序（先写产物再入库）就把那条错误
        #   丢在了 run.json 之外。
        self._save_to_db(record, structured=structured)
        record.errors = list(self.errors)
        await self._write_artifacts(record, structured=structured, history=history,
                                    recorder=recorder)

        return RunOutcome(
            run_id=self.run_id,
            run_dir=self.run_dir,
            status=status,
            parse_status=parse_status,
            rows_collected=record.rows_collected,
            record=record,
            steps=list(self.step_records),
        )

    # ── 一次 attempt ──────────────────────────────────────
    async def _run_once(
        self, *, llm: Any, tools: Any, recorder: RunRecorder, attempt: int
    ) -> "_AttemptResult":
        from browser_use import Agent

        from ecom_agent.runtime.browser import browser_session

        self._history_len = 0

        # ★ 每个 attempt 一个新的 policy + interceptor：
        #   · policy 带着"连续被拦"计数器。跨 attempt 累积的话，第二次尝试
        #     会在第一步就因为**上一次**撞了三次护栏而被硬停，
        #     而硬停的说明写的是"LLM 在反复尝试同一个不该做的动作" ——
        #     那句话对新一次的 LLM 是**不成立**的，报告会指向错误的原因。
        #   · interceptor 的 stop_reason / aborted_step 同理，都是 attempt 级的。
        policy = GuardrailPolicy(self.spec.guardrails)
        interceptor = GuardrailInterceptor(
            policy,
            self.approver,
            recorder=recorder,
            run_id=self.run_id,
            redactor=self.redactor,
            tools=tools,
        )

        async with browser_session(
            self.compiled, warmup_url=self.spec.start_url
        ) as session:
            agent = Agent(
                task=self.compiled.task_text,
                llm=llm,
                browser_session=session,
                tools=tools,
                output_model_schema=self.compiled.output_model,
                register_new_step_callback=interceptor.on_new_step,
                # ★ 硬停靠这条通道，不靠注入一个 done 动作（理由见 interceptor.should_stop）
                register_should_stop_callback=interceptor.should_stop,
                **self.compiled.agent_kwargs,
            )
            self._last_agent = agent

            # ★★ 接线自检必须在 run 之前。护栏的拒绝是"往动作列表里塞一个
            #    guard_notice"，而那个动作是 Agent 构造时按注册表生成的。
            #    接线不对时判定照做、记录照写、**动作照跑** —— 看起来在跑。
            # ★ 必须 await：它除了查动作模型，还要**真的 await 一次**
            #   `should_stop` —— 库要求那个回调是 Awaitable，同步版会让
            #   每一步都抛 'bool' object can't be awaited（详见其 docstring）。
            await interceptor.check_wiring(agent)

            try:
                history = await agent.run(
                    on_step_end=self._make_step_hook(recorder, interceptor),
                    **self.compiled.run_kwargs,
                )
            finally:
                await interceptor.aclose()

            structured, error = _extract_structured(history, self.compiled.output_model)
            if error:
                self.errors.append(error)

            rows = _row_count(structured)

            return _AttemptResult(
                history=history,
                structured=structured,
                product_rows=rows,
                blocked=interceptor.stop_reason is not None,
                stop_reason=interceptor.stop_reason or "",
            )

    def _make_step_hook(self, recorder: RunRecorder, interceptor: GuardrailInterceptor):
        """造 `on_step_end` 钩子。

        ★★ 这里是全文最微妙的一处，值得逐条说清：

        库在 `_execute_step` 里**无条件**调 `on_step_end`（service.py:2481-2482），
        也就是 `self.step()` 返回之后无论成功、失败、被中断都会调。
        而"这一步有没有产生一条历史"是**另一件事**：

            `step()` 的 finally 里调 `_finalize`（service.py:1085-1086），
            而 `_finalize` 的第一行是 `if not self.state.last_result: return`
            （service.py:1357-1358）—— **直接返回，不追加历史项，也不推进 n_steps**。

        什么时候 `last_result` 是空的？至少两种，都是真实路径：
            · 护栏硬停：InterruptedError 在 `_execute_actions` 之前就被抛出，
              被 `step()` 的 except 吞掉（`_handle_step_error` 对 InterruptedError
              只打一条 warning 就 return），于是 `last_result` 保持为
              `_prepare_context` 之后被清空的那个 None（service.py:1068-1069）。
            · 动作列表为空：`multi_act([])` 返回 `[]`，同样是 falsy。

        这些情况下 `history.history[-1]` 指的是**上一步**。直接记，就是把上一步
        记两遍 —— 盘上多一行、少一行，而重复的那一行内容完全合法：
        有 URL、有截图、有动作、有结果。**没有任何一处会报错。**

        所以闸门是"列表有没有变长"，而不是"钩子有没有被调"。
        变长了才记，并且记的是严格意义上的最后一项。
        """
        async def _hook(agent: Any) -> None:
            history = agent.history.history
            current = len(history)
            if current <= self._history_len:
                # ★ 收下 interceptor 攒的判定：这一步没产生历史，
                #   但判定确实发生过。丢掉的话，"护栏判了什么"会少一条记录，
                #   而少掉的那条恰好在硬停路径上 —— 最需要证据的地方。
                dropped = interceptor.take_decisions()
                if dropped:
                    logger.debug(
                        "step 未产生历史项（被中止或动作为空），丢弃 %d 条判定记录：%s",
                        len(dropped), [d.rule_id for d in dropped],
                    )
                return
            self._history_len = current
            await self._record_step(
                recorder, interceptor, item=history[-1], fallback_step=agent.state.n_steps
            )

        return _hook

    async def _record_step(
        self,
        recorder: RunRecorder,
        interceptor: GuardrailInterceptor,
        *,
        item: Any,
        fallback_step: int,
    ) -> None:
        """把一个新的历史项写成一行 steps.jsonl。"""
        # ★ 步号取历史项自己的 metadata.step_number —— 那是库记账用的数字，
        #   和 new_step_callback 拿到的 step_index 同源（都是 state.n_steps），
        #   所以两边的记录能对上。自己维护计数器的话，一旦库在某条路径上
        #   跳号或补号，我们和它的编号会悄悄分叉。
        meta = getattr(item, "metadata", None)
        step_index = int(getattr(meta, "step_number", 0) or 0) or fallback_step

        duration = 0.0
        if meta is not None:
            try:
                duration = max(
                    0.0, float(meta.step_end_time) - float(meta.step_start_time)
                )
            except (TypeError, ValueError, AttributeError):
                duration = 0.0

        # ★ actions_not_executed：本步动作一个都没执行（护栏硬停中止了它）。
        #   ⚠️ 老实说：按上面的分析，"被中止的步"根本不会走到这里
        #   （`_finalize` 早退 → 历史不增长 → 闸门挡掉）。所以这个字段在当前
        #   库版本上是**防御性**的，正常情况下恒为 False。
        #   留着它的理由有两条，都不是"万一"：
        #     1. 那条推理链压在 `_finalize` 的早退条件上，而它是库的内部实现 ——
        #        它一变（比如改成先追加再判空），闸门就挡不住了，
        #        而那时**这一行是唯一还能指出"结果不可信"的东西**。
        #     2. 报告里那一栏的渲染逻辑（`_render_step`）需要它才能被测试覆盖。
        not_executed = interceptor.aborted_step == step_index

        record = await recorder.record_step(
            item,
            step_index=step_index,
            duration_s=duration,
            decisions=interceptor.take_decisions(),
            tokens=(self.usage.prompt_tokens, self.usage.completion_tokens),
            actions_not_executed=not_executed,
        )
        self.step_records.append(record)

    # ── 汇总 ──────────────────────────────────────────────
    def _build_record(
        self,
        *,
        status: str,
        parse_status: str,
        structured: Any,
        history: Any,
        stop_reason: str,
        started: float,
        recorder: RunRecorder,
    ) -> RunRecord:
        if stop_reason:
            self.errors.append(f"护栏硬停：{stop_reason}")

        flags: dict[str, list[str]] = {}
        raw = ""
        if structured is not None:
            raw = structured.model_dump_json(indent=2)
            # ★ all_sanity_flags 只标记、不删除（见 output_models 的说明）
            flags = structured.all_sanity_flags() if hasattr(structured, "all_sanity_flags") else {}
        elif history is not None:
            # quarantine：拿不到合法模型，但**原文必须留着** ——
            # 那份原文恰恰是最该看的（见 RunRecorder.write_result 的说明）
            raw = history.final_result() or ""

        stats = recorder.observation_stats()
        return RunRecord(
            run_id=self.run_id,
            task_id=self.compiled.task_id,
            task_name=self.spec.name,
            attempt=self.attempts,
            status=status,
            # ★ started_at 用**开跑前**记下的那个时刻，不是这里 now_iso()。
            #   在收尾处取"开始时间"会让 duration 和两个时间戳互相对不上，
            #   而报告里那三样是并排显示的 —— 对不上看起来像时钟有问题。
            started_at=self._started_at,
            finished_at=now_iso(),
            duration_s=round(time.time() - started, 3),
            task_fingerprint=self.compiled.fingerprint,
            browser_use_version=compat.EXPECTED_BROWSER_USE_VERSION,
            model=_llm_model_name(self._last_agent),
            provider=_llm_provider(self._last_agent),
            params=self.compiled.params,
            start_url=self.spec.start_url,
            # ★ 完整存下"下发给 LLM 的字节"。回放时唯一能回答
            #   "LLM 当时到底看到了什么"的东西 —— 靠任务+参数反推是不行的，
            #   拼装逻辑改过之后反推出来的就不是当时那份。
            compiled_task_text=self.compiled.task_text,
            guardrail_policy=self.spec.guardrails.model_dump(mode="json"),
            steps=len(self.step_records),
            llm=self.usage,
            parse_status=parse_status,
            rows_collected=_row_count(structured),
            sanity_flags=flags,
            result_raw=raw,
            # ★ 调试后门用过的痕迹。取自 approver 的类属性 ——
            #   没有这个字段的话，"我调试时关掉审批跑过一次"和"这次真的没人需要审批"
            #   在记录里长得一模一样，而前者是必须在真实场景里被发现的。
            unsafe_auto_approved=bool(getattr(self.approver, "unsafe", False)),
            errors=list(self.errors),
            **stats,
        )

    async def _write_artifacts(
        self, record: RunRecord, *, structured: Any, history: Any, recorder: RunRecorder
    ) -> None:
        # ★ result.json **无条件**写，哪怕是空的。
        #   理由：产物清单是可被机器读的契约（artifacts 字典、Web 层的文件路由、
        #   `tail -1` 式的排查习惯）。让它"成功时才有"意味着每个消费者都要
        #   处理"这个文件可能不存在"这一分支 —— 而分支的默认实现通常就是**跳过**，
        #   于是"这次 run 什么都没产出"和"这个文件没写出来"永远分不清。
        #   空文件是诚实的："这次没有产出可存的东西"。
        await recorder.write_result(record.result_raw)

        # ★ 顺序：先把报告**渲染并落盘**，再声明 artifacts，最后写 run.json。
        #   反过来的话，render_report 一旦抛错，盘上就已经有了一份
        #   "artifacts 里写着 report.json/report.html 存在"的 run.json ——
        #   而那个文件并不存在。产物清单是给机器读的契约，
        #   它声明的东西必须已经躺在那里（或者干脆不声明）。
        html = render_report(record, self.step_records, run_dir=self.run_dir)
        write_report(recorder.report_path, html)

        record.artifacts = recorder.artifacts()
        record.artifacts["report.html"] = "report.html"
        await recorder.write_run_record(record)
        logger.info("产物已写入 %s", self.run_dir)

    def _save_to_db(self, record: RunRecord, *, structured: Any) -> None:
        """落 SQLite。★ 失败只记日志，不让整个 run 变成失败。

        理由：产物（run.json / steps.jsonl / 截图 / 报告）已经落盘，
        它们才是审计的原始凭证。一个"入库失败"不该让这些**已经存在的证据**
        被标成无效 —— 反过来，入库成功而产物缺失才是更该担心的。
        但必须留痕：silent 的入库失败会让 Web 层显示出一个空的历史列表。
        """
        from ecom_agent.store.repository import Repository

        try:
            rows = list(getattr(structured, "rows", []) or []) if structured is not None else []
            with Repository(DB_PATH) as repo:
                repo.save_run(record, rows, redactor=self.redactor)
        except Exception as exc:  # noqa: BLE001
            logger.exception("落库失败（产物已落盘，不影响本次运行的有效性）")
            self.errors.append(f"落库失败：{type(exc).__name__}: {exc}")


# ── attempt 的内部结果 ────────────────────────────────────
@dataclass
class _AttemptResult:
    history: Any
    structured: Any
    product_rows: int
    blocked: bool
    stop_reason: str
    exception: Exception | None = None


# ── 小工具（纯函数，可离线测）─────────────────────────────
def _extract_structured(history: Any, output_model: type) -> tuple[Any, str]:
    """从 history 取结构化输出。返回 `(模型实例或 None, 错误说明)`。

    ★★ 这里必须 catch `ValidationError` 而**不是**让它冒出去：
      `history.get_structured_output(Model)` 内部直接
      `output_model.model_validate_json(final_result)`（views.py:938-948），
      也就是**校验失败会抛**。而"LLM 产出的 JSON 不符合 schema"正是
      四档容错里的第 4 档（quarantine）要处理的情况 —— 它是**预期内的结局**，
      不是崩溃。让它冒出去会让一次"schema 漂移"变成"整个 run 抛异常"，
      而那时 result.json 和报告都不会被写出来，恰恰丢掉了最该看的那份原文。

    ★ 这里也**不用** `history.structured_output`（那个 property 更直观）：
      它依赖私有字段 `_output_model_schema`，序列化后丢失 → 存盘读回时静默返回
      None。get_structured_output(model) 把 model 当参数传，不依赖那个字段。
    """
    if history is None:
        return None, ""
    final = history.final_result()
    if final is None:
        return None, "LLM 没有产出最终结果（未调用 done？）"
    try:
        return compat.get_structured_output(history, output_model), ""
    except ValidationError as exc:
        # 只留前几行：pydantic 的完整报错可能上百行，塞进 run.json 会盖住别的东西。
        head = "\n".join(str(exc).splitlines()[:12])
        return None, f"结构化输出不符合 schema（quarantine，未入库）：{head}"
    except Exception as exc:  # noqa: BLE001
        return None, f"解析结构化输出失败：{type(exc).__name__}: {exc}"


def _row_count(structured: Any) -> int:
    rows = getattr(structured, "rows", None)
    try:
        return len(rows) if rows is not None else 0
    except TypeError:
        return 0


def _llm_model_name(agent: Any) -> str:
    llm = getattr(agent, "llm", None)
    return str(getattr(llm, "model", "") or "")


def _llm_provider(agent: Any) -> str:
    llm = getattr(agent, "llm", None)
    return str(getattr(llm, "provider", "") or "")


# ── 对外入口 ──────────────────────────────────────────────
async def run_task(
    compiled: CompiledTask,
    *,
    approver: Any | None = None,
    runs_dir: Path | str | None = None,
    run_id: str | None = None,
    llm: Any | None = None,
) -> RunOutcome:
    """跑一次任务。这是 CLI / Web 层唯一的调用点。"""
    return await TaskRunner(
        compiled,
        approver=approver,
        runs_dir=runs_dir,
        run_id=run_id,
        llm=llm,
    ).run()
