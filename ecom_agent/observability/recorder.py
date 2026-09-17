"""RunRecorder —— 把一次 run 的过程落成可回放的产物。

产物清单（`runs/{run_id}/`）：

    run.json         一次 run 的头部事实（含完整下发的任务文本、护栏策略快照）
    steps.jsonl      每步一行：动作 / 结果 / 判定 / 截图路径 / 耗时
    result.json      结构化输出（或 quarantine 时的原始产出）
    screenshots/     每步一张 PNG（库自己落的图在系统临时目录，关机即失）
    report.html      给人看的报告

★ 两条纪律，都来自实测踩出来的坑，不是洁癖：

  1. **快照必须当场取值。** `browser_state` 里的 `selector_map` 与会话内部缓存
     是同一个 dict，`reset()` 会原地清空它（S2-4）。所以本类的接口**只收
     `BrowserSnapshot`（纯值）**，不收 `browser_state` —— 拿不到库对象，
     "晚点再读"这件事在类型上就做不到。取值本身在 `compat.snapshot_browser_state()`。

  2. **所有落盘 IO 都走 `asyncio.to_thread`。** 这些方法在 agent 的步进回调里被 await；
     直接在事件循环里写文件（尤其是一张几百 KB 的截图）会卡住 CDP 心跳。
     这条的代价已经在 `CliApprover` 上见过一次了（同步 `input()` 卡死心跳）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Iterable

from ecom_agent import compat
from ecom_agent.observability.models import (
    ActionRecord,
    BrowserSnapshot,
    GuardrailDecisionRecord,
    ResultRecord,
    RunRecord,
    StepRecord,
    now_iso,
)
from ecom_agent.observability.redact import Redactor

logger = logging.getLogger(__name__)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
"""★ 判"这是不是一张真 PNG"，而不是"长度大于 0"。

长度大于 0 极易满足：一个被转坏的 base64、一段报错文本、一个 0 字节文件
都能让它"看起来有截图"。而截图是回放的最终交付物 ——
坏图必须在这一层就被发现，不能等到打开 report.html 时才发现
（那时已经不知道是哪一步、哪条路径坏的）。
"""

RUN_JSON = "run.json"
STEPS_JSONL = "steps.jsonl"
RESULT_JSON = "result.json"
REPORT_HTML = "report.html"
SCREENSHOT_DIR = "screenshots"


class RunRecorder:
    """一次 run 的记录器。一个 run 一个实例。"""

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        run_dir: Path,
        redactor: Redactor | None = None,
        screenshot: bool = True,
        record_llm_io: bool = True,
        attempt: int = 1,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.run_dir = Path(run_dir)
        self.attempt = attempt
        self.screenshot = screenshot
        self.record_llm_io = record_llm_io
        self.redactor = redactor or Redactor()

        self.screenshots_dir = self.run_dir / SCREENSHOT_DIR
        self.steps_path = self.run_dir / STEPS_JSONL
        self.run_json_path = self.run_dir / RUN_JSON
        self.result_path = self.run_dir / RESULT_JSON
        self.report_path = self.run_dir / REPORT_HTML

        # ── 回调 ⇄ 钩子 的交接槽 ─────────────────────────
        # ★ 只有一个槽，不是 dict：回调与钩子本应一一交替。
        #   用 dict 按 step_index 索引的话，"索引对不齐"会表现为**查不到**，
        #   于是落到"没快照"这个正常分支里 —— 又是一次静默失效。
        #   单槽 + 覆盖计数让错位变成一个能看见的数字。
        self._pending: BrowserSnapshot | None = None
        self._pending_shot: str | None = None
        self._pending_step: int | None = None

        # ── 观测自身的健康度计数 ──────────────────────────
        self.snapshot_overwrites = 0
        self.overwritten_steps: list[int] = []
        """★ 被覆盖掉快照的那些步（记的是**受害者**的 step，不是覆盖者的）。

        为什么要记到步一级，而不只有 `snapshot_overwrites` 那个总数：
          总数告诉你有错帧，但看 `steps.jsonl` 第 N 行的人无法判断**第 N 行**
          是不是那个错帧 —— 而错帧的记录看起来完全正常（有 URL、有图、有动作）。
          所以 `StepRecord.snapshot_overwritten` 需要按步去查，这里就是那份名单。
        """
        self.snapshot_missing_steps: list[int] = []
        self.same_frame_steps: list[int] = []
        self.screenshot_count = 0
        self.broken_screenshot_steps: list[int] = []
        self.empty_selector_map_steps: list[int] = []
        """★ selector_map 为空的那些步。

        ★★ 这是 S2-4 那个静默失效的**探测器**。库把 map 清空时，
          护栏拿不到元素文本 → 没有规则命中 → 而"没有命中"是合法结果。
          把它记成一个数字之后，"护栏其实一步都没判"就不再是隐形的了。
        """

        self._last_sha: str | None = None
        self._steps_written = 0

        for d in (self.run_dir, self.screenshots_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── 重试：切换 attempt ────────────────────────────────
    def begin_attempt(self, attempt: int) -> None:
        """切到第 N 次尝试。★ 一个 run 一个记录器，attempt 之间靠这个切换。

        ★★ 为什么不是"每个 attempt 新建一个 RunRecorder"：
          本类的计数器（截图数、覆盖次数、空的 selector_map 步、脱敏命中）都是
          **run 级健康度**。每个 attempt 换一个实例的话，这些数字只剩最后一次
          attempt 的 —— 而"第一次尝试时观测是不是坏的"恰恰是重试场景下
          最该看的信息（第一次失败的原因往往就在那些数字里）。

        ★ 切 attempt 会清掉交接槽：上一个 attempt 的残留快照如果被这一次消费，
          记录里会出现一行"属于 attempt 2、却是 attempt 1 的页面"的记录。
          跨 attempt 的错帧比同 attempt 内的更隐蔽 —— 因为两次的页面
          通常长得就不一样，读者会当成"重试后页面变了"。
        """
        self.attempt = int(attempt)
        self._pending = None
        self._pending_shot = None
        self._pending_step = None
        # ★ `_last_sha` 也要清：它的语义是"和**上一步**是不是同一帧"，
        #   而跨 attempt 的那一对截图之间没有"上一步"的关系。
        #   不清的话，重试的第一步会被标成"画面与上一步相同"——
        #   一个真实存在的误报，而且它指向的是两个不同 attempt 的图。
        self._last_sha = None

    # ── 回调侧：当场取值 ──────────────────────────────────
    def stash_snapshot(
        self, fields: dict[str, Any], screenshot_b64: str | None, *, step_index: int
    ) -> None:
        """把**当场取好的纯值**放进交接槽。由 `new_step_callback` 调用。

        ★ 签名里收的是 dict 而不是 `browser_state` —— 这是刻意的：
          接口上就拿不到库对象，所以不可能写出"先存着晚点读"的代码。
        """
        if self._pending is not None:
            self.snapshot_overwrites += 1
            # ★ 记的是【被覆盖的那一步】。覆盖者（现在的 step_index）自己的快照
            #   好好地躺在槽里，没有丢失；丢的是 `_pending_step` 那一份。
            #   记错了对象的话，报告会指着一个完好的步说"它有错帧"。
            if self._pending_step is not None:
                self.overwritten_steps.append(self._pending_step)
            logger.warning(
                "step %s 的快照覆盖了尚未消费的 step %s 快照 —— "
                "回调与钩子的配对时序和假设不符，这些步的记录可能整体错位一帧",
                step_index,
                self._pending_step,
            )
        self._pending = BrowserSnapshot(**fields)
        self._pending_shot = screenshot_b64
        self._pending_step = step_index

        if not fields.get("selector_map_size"):
            # 索引区为空：要么页面真没元素，要么库把它清了。两种都要能看见。
            self.empty_selector_map_steps.append(step_index)
            logger.warning("step %s 的 selector_map 为空 —— 护栏这一步拿不到元素文本", step_index)

    # ── 钩子侧：写一行 ────────────────────────────────────
    async def record_step(
        self,
        history_item: Any,
        *,
        step_index: int,
        duration_s: float = 0.0,
        decisions: Iterable[GuardrailDecisionRecord] = (),
        tokens: tuple[int, int] = (0, 0),
        actions_not_executed: bool = False,
    ) -> StepRecord:
        """记录一步。由 `on_step_end` 调用（async —— 见文件头的纪律 2）。"""
        snapshot, shot = self._take_pending()
        facts = compat.extract_step_facts(history_item)

        shot_path, shot_sha = await self._persist_screenshot(step_index, shot)
        same_frame = shot_sha is not None and shot_sha == self._last_sha
        if shot_sha is not None:
            self._last_sha = shot_sha
        if same_frame:
            self.same_frame_steps.append(step_index)

        record = StepRecord(
            run_id=self.run_id,
            task_id=self.task_id,
            attempt=self.attempt,
            step=step_index,
            duration_s=round(duration_s, 3),
            url=snapshot.url if snapshot else facts["url"],
            title=snapshot.title if snapshot else facts["title"],
            actions=[
                ActionRecord(
                    name=a["name"],
                    params=a["params"],
                    element_text=self._text_for(snapshot, a["params"]),
                )
                for a in facts["actions"]
            ],
            results=[ResultRecord(**r) for r in facts["results"]],
            guardrail_decisions=list(decisions),
            model_thought=facts["thought"] if self.record_llm_io else "",
            tokens_in=tokens[0],
            tokens_out=tokens[1],
            screenshot_path=shot_path,
            screenshot_sha256=shot_sha,
            same_frame_as_previous=same_frame,
            snapshot_missing=snapshot is None,
            snapshot_overwritten=step_index in self.overwritten_steps,
            actions_not_executed=actions_not_executed,
        )

        if snapshot is None:
            self.snapshot_missing_steps.append(step_index)

        # ★ 脱敏发生在【写盘之前】，不是写完再洗。
        #   写完再洗意味着盘上曾经存在过一份未脱敏的内容，而"曾经存在过"
        #   在崩溃中断、别的进程正在 tail、文件系统写前日志 三种情况下都会留下痕迹。
        payload = self.redactor.obj(record.model_dump(mode="json"))
        await asyncio.to_thread(self._append_jsonl, self.steps_path, payload)
        self._steps_written += 1
        return record

    def _take_pending(self) -> tuple[BrowserSnapshot | None, str | None]:
        """取走交接槽里的快照并清空它，返回 (快照, 截图 base64)。

        ★ 取走就清空（而不是留着）：留着的槽会让"上一步的快照被这一步复用"
          变成可能，而那种错帧在报告里看起来完全正常 —— 有 URL、有图、有动作。
        """
        snap, shot = self._pending, self._pending_shot
        self._pending = None
        self._pending_shot = None
        self._pending_step = None
        return snap, shot

    @staticmethod
    def _text_for(snapshot: BrowserSnapshot | None, params: dict[str, Any]) -> str | None:
        """这一步的动作落在哪个元素上（人话）。

        ★ 拿不到就返回 None，而 None 是一个**有意义的值**：
          护栏的 match_element_text 在拿到 None 时判不命中（fail-safe 方向）。
          记录里成片出现 None，就是在告诉你"有一批规则在这些页面上是空转的"。
        """
        if snapshot is None:
            return None
        idx = params.get("index")
        if idx is None:
            return None
        try:
            return snapshot.element_texts.get(int(idx))
        except (TypeError, ValueError):
            return None

    # ── 截图 ──────────────────────────────────────────────
    async def _persist_screenshot(self, step: int, b64: str | None) -> tuple[str | None, str | None]:
        """把回调里拿到的 base64 截图落成文件，返回 (相对路径, sha256)。

        ★ 为什么必须自己另存：库自己落的图在**系统临时目录**
          （`agent_directory`，service.py:448-449）—— 关机即失。
          S5 把这条变成了可执行断言（`Path(p).is_relative_to(tempfile.gettempdir())`），
          不是只在文档里写一句"要另存"。

        ★ 不是真 PNG 就【不写文件】：
          写下去会得到一个看起来正常、打开是坏图的产物，而且它混在一堆好图里。
          宁可这一步"没有截图"并在 run.json 里多一个计数 ——
          **缺席是诚实的，坏图是骗人的。**
        """
        if not self.screenshot or not b64:
            return None, None
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception:  # noqa: BLE001
            self.broken_screenshot_steps.append(step)
            logger.warning("step %s 的截图 base64 解不开，跳过", step)
            return None, None

        if raw[:8] != PNG_MAGIC:
            self.broken_screenshot_steps.append(step)
            logger.warning(
                "step %s 的截图不是 PNG（前 8 字节 %r），不落盘 —— "
                "宁可这一步没有截图，也不产生一个打开才发现坏掉的产物",
                step,
                raw[:8],
            )
            return None, None

        rel = f"{SCREENSHOT_DIR}/a{self.attempt:02d}/step-{step:03d}.png"
        target = self.run_dir / rel
        # ★★ 截图按 attempt 分子目录。这不是整洁癖 —— 不分的后果是**静默覆盖**：
        #   重试的第 2 次从 step=1 重新编号，于是 attempt-1 的图被 attempt-2 的图
        #   覆盖掉，而两次记录里的路径都写着 `screenshots/step-001.png`。
        #   报告看起来完全正常（有图、有路径、能打开），只是 attempt-1 的那张
        #   已经不在了 —— 而那正是排查"第一次为什么失败"唯一想看的东西。
        #   和本项目其他几处是同一条纪律：产物要么是真当时的，要么明确缺席。
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, raw)
        self.screenshot_count += 1
        return rel, hashlib.sha256(raw).hexdigest()

    # ── 汇总产物 ──────────────────────────────────────────
    async def write_run_record(self, record: RunRecord) -> Path:
        """写 run.json。★ 同样在写盘之前脱敏。"""
        payload = self.redactor.obj(record.model_dump(mode="json"))
        await asyncio.to_thread(self._write_json, self.run_json_path, payload)
        return self.run_json_path

    async def write_result(self, raw_json: str) -> Path:
        """写 result.json —— 结构化输出的原文。

        ★ 存**原文**而不是再序列化一遍模型对象：quarantine 时（schema 不合法）
          根本没有合法的模型对象可存，而那份原文恰恰是最该留的东西。
          同一份代码路径覆盖"成功"和"失败"两种情形，就不会出现
          "失败路径没写产物"这种事。
        """
        await asyncio.to_thread(self._write_text, self.result_path, raw_json)
        return self.result_path

    def observation_stats(self) -> dict[str, Any]:
        """观测自身的健康度 —— 由 runner 并进 run.json。

        ★★ 这里的每个键都必须**在 RunRecord 上有对应字段**。这个函数的返回值是
          `RunRecord(**stats)` 形式的合并，多出来的键在 `extra="forbid"` 下会直接
          报错（那是好事），但**漏掉的键会静默消失** —— 比如
          `empty_selector_map_steps` 一度只在这里被计数、却没有任何字段接住它，
          于是 S2-4 的探测器算出来了，而没有人会看到。
          计数器算出来却没人看得到，和没算是同一件事 ——
          只是更难发现，因为代码看起来是在防这件事的。
        """
        return {
            "screenshot_count": self.screenshot_count,
            "same_frame_steps": list(self.same_frame_steps),
            "snapshot_missing_steps": list(self.snapshot_missing_steps),
            "snapshot_overwrites": self.snapshot_overwrites,
            "empty_selector_map_steps": list(self.empty_selector_map_steps),
            "redaction_counts": dict(self.redactor.counts),
        }

    def artifacts(self) -> dict[str, str]:
        """产物清单。★ 报告/Web 层据此找文件，不各自拼路径。"""
        out = {STEPS_JSONL: STEPS_JSONL, RUN_JSON: RUN_JSON}
        if self.result_path.exists():
            out[RESULT_JSON] = RESULT_JSON
        if self.screenshots_dir.exists() and any(self.screenshots_dir.iterdir()):
            out[SCREENSHOT_DIR] = SCREENSHOT_DIR
        return out

    @property
    def steps_written(self) -> int:
        return self._steps_written

    # ── 文件原语 ──────────────────────────────────────────
    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        """追加一行。

        ★ 每次开-写-关，而不是持有一个句柄：
          句柄被 buffer 着，崩在两步之间就丢掉最后几步 ——
          而那恰恰是崩溃现场最想看的几步。
          开-写-关的代价是每步一次 open()，对一个每步都要截图的流程来说可以忽略。
          （这也正是 steps.jsonl 用 JSONL 而不是一个大 JSON 数组的理由：
           数组必须写完整才能解析，而长 run 恰恰是最可能崩的那种。）
        """
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.write("\n")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")


def new_run_id() -> str:
    """生成 run_id：UTC 时间戳 + 短随机后缀。

    ★ 前缀是时间戳是为了**能排序**（ls 出来就是时间顺序），
      后缀是随机的为了同一秒内并发起两个 run 也不会撞。
      纯 uuid 做不到第一条，纯时间戳做不到第二条。
    """
    return f"{now_iso().replace(':', '').replace('-', '')}-{secrets.token_hex(3)}"
