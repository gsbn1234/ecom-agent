"""GuardrailInterceptor —— 把策略引擎挂到 agent 的步进流程上。

挂在 `register_new_step_callback`（时序：**LLM 输出之后、动作执行之前**，
`agent/service.py:1712-1724`，由 `step()` → `_get_next_action()` 在第 1206 行触发）。

这个挂点满足三个条件，每一条都对应一个已核实的引擎事实：
  1. **能真正 await 人工且不终止 run** —— 回退方案（停机重跑）做不到；
  2. **不受 `Tools.act` 的 180s 超时约束**（`tools/service.py:2196`）——
     审批发生在这个回调里，不在 action 函数体内。
     ⚠️ 这条极重要：否则人工还没点批准，action 先被掐了，表现为一个莫名其妙的
     `ActionResult(error=...)`，从报错完全看不出是超时配置问题；
  3. **能就地改写 `model_output.action`** —— 它就是 `state.last_model_output`
     同一个对象，`_execute_actions` 随后读的就是它。而且**库自己就这么干**
     （`service.py:1690-1698` 造 noop action），所以这不是野路子。

本文件承担一条硬约束（S2-4，见 docs/spikes.md）：
  **必须在拿到 `browser_state` 的那一刻把要用的值取走。**
  这里做的正是这件事 —— `_stash()` 立刻调 `compat.snapshot_browser_state()`
  并把纯值交给 recorder。之后本文件**不再持有** `browser_state`。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Iterable

from ecom_agent import compat
from ecom_agent.actions.guard_gate import (
    GUARD_NOTICE_ACTION,
    make_notice_action,
    verify_notice_round_trip,
)
from ecom_agent.guardrails.approver import ApprovalRequest, Approver
from ecom_agent.guardrails.policy import DecisionResult, GuardrailPolicy
from ecom_agent.observability.events import EventBus, RunEvent
from ecom_agent.observability.models import GuardrailDecisionRecord, now_iso
from ecom_agent.observability.redact import Redactor

logger = logging.getLogger(__name__)

TERMINAL_ACTION = "done"
"""终止动作名。★ 它被**豁免**于护栏判定，理由见 `_evaluate_one` 的说明。"""

EXEMPT_RULE_ID = "exempt:done"


class GuardrailInterceptor:
    """一次 run 的护栏执行器。"""

    def __init__(
        self,
        policy: GuardrailPolicy,
        approver: Approver | None = None,
        *,
        recorder: Any | None = None,
        run_id: str = "",
        redactor: Redactor | None = None,
        tools: Any | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.policy = policy
        self.approver = approver
        self.recorder = recorder
        self.run_id = run_id
        self.redactor = redactor or Redactor()
        self._page_changing = compat.page_changing_actions(tools) if tools is not None else set()

        # ★ 实时事件通道（Phase 5）。可以没有 —— 没有时所有发布点静默跳过，
        #   行为与加它之前完全一致。这条"可选"是刻意的：CLI 跑批、离线单测
        #   都不需要看板，不该被迫先建一个总线。
        self.events = events

        # 本步的判定，等 on_step_end 时交给 recorder。
        # ★ 用实例状态而不是"给回调返回值"：new_step_callback 的返回值被库忽略
        #   （service.py:1713-1724 不接返回值），而步进记录发生在另一个挂点
        #   （on_step_end），两者只能通过实例状态交接。
        self._pending_decisions: list[GuardrailDecisionRecord] = []

        # 硬停标志：连续被拦到上限。
        self.stop_reason: str | None = None

        # 被硬停中止的那一步。★ 为什么单独记：
        #   被中止的那一步**照样会被 on_step_end 记进 steps.jsonl**
        #   （`_execute_step` 在 `self.step()` 之后无条件调 on_step_end，
        #   见 service.py:2481-2482 —— InterruptedError 被 step() 自己吞了）。
        #   而它的 `results` 是**上一步的残留**（`_finalize` 用的是
        #   `state.last_result`，而本步的 `_execute_actions` 根本没跑）。
        #   不把这个步号传下去，报告里就会出现一行"动作是 guard_notice、
        #   结果却来自上一步"的记录 —— 看起来完全正常，但是错的。
        self.aborted_step: int | None = None

        # 最近一次审批是谁批的。★ 必须记住原因：被拒的说明里要写清"是人工拒的、
        # 还是根本没有通道"，而这两种在 steps.jsonl 里长得几乎一样。
        self._last_approver: str = ""

        # `agent.ActionModel`，由 bind_action_model 从 agent 上取。
        self._action_model: Any = None

    # ── 接线自检 ──────────────────────────────────────────
    async def check_wiring(self, agent: Any) -> None:
        """构造完 Agent、**跑起来之前**调一次。接线不对就当场报错。

        ★ 为什么值得为此中断启动：
          拒绝一个动作的实现方式，是往 `model_output.action` 里塞一个 `guard_notice`。
          而那个动作是 Agent 构造时按注册表动态生成的。接线不对时，
          判定照做、记录照写，**动作照跑** —— 护栏看起来在跑。

        ★★ 检查方式是**端到端往返**，不是"看字段名在不在"：
          字段名那条路我踩过，会误报（`agent.ActionModel` 在多动作时是
          `RootModel[Union[...]]` 包装，`model_fields` 只有 `{'root'}`）。
          详见 `actions/guard_gate.verify_notice_round_trip` 的说明。

        ★ 两个检查的**失败后果不同，所以两个都要**：
          · `verify_notice_round_trip` —— 拒绝路径坏了：护栏看起来在跑，动作照跑。
          · `verify_stop_callback`   —— 硬停路径坏了：每步都报错，run 一步都跑不完。
          它们是两条独立的路径，坏掉的表现也完全不同，所以不能用一个代表另一个。
          后者是 live 验收跑出来的实锤，之前只有前者。
        """
        self.bind_action_model(agent)
        verify_notice_round_trip(self._action_model)
        await self.verify_stop_callback()

        # 顺带把"动作模型里到底有哪些动作"打出来 —— 库改版时这是第一眼要看的东西。
        fields = compat.action_model_fields(self._action_model)
        if not fields:
            logger.warning(
                "拿不到动作模型的字段列表（库可能改版）—— 往返自检已经通过，"
                "所以拒绝路径是好的；但这份诊断信息不可用，排查时请人工确认。"
            )
        else:
            logger.debug("动作模型共 %d 个动作，含 %s", len(fields), GUARD_NOTICE_ACTION)

    # ── 主入口：挂在 register_new_step_callback ────────────
    async def on_new_step(
        self,
        browser_state: Any,
        model_output: Any,
        step_index: int,
    ) -> None:
        """判定本步的全部动作，必要时就地改写它们。"""
        # ── 1. 当场取值（S2-4 硬约束）─────────────────────
        actions = list(_iter_actions(model_output))
        fields, shot = compat.snapshot_browser_state(
            browser_state, indices=_action_indices(actions)
        )
        if self.recorder is not None:
            self.recorder.stash_snapshot(fields, shot, step_index=step_index)

        url = fields["url"]
        element_texts: dict[int, str] = fields["element_texts"]

        # ── 2. 逐个判定 ────────────────────────────────────
        kept: list[Any] = []
        changed = False
        for original, name, params in actions:
            if self.stop_reason is not None:
                # 已经决定硬停了。剩下的动作既不再判定也不问人 ——
                # 因为本次的 action 列表下面会被整个替换成一条 done，
                # 继续问人只会让用户在 run 已经结束之后还收到审批卡片。
                logger.debug("已决定硬停，跳过剩余动作的判定：%s", name)
                break

            element_text = _text_for(element_texts, params)
            outcome = await self._evaluate_one(name, params, url, element_text, step_index)
            # ★ 判定记录**无论结果如何都要收**：允许的动作也留下了证据
            #   （"这一步判过、结论是放行"和"这一步没判过"是两条不同信息）。
            self._pending_decisions.append(outcome.record)

            if outcome.approved:
                kept.append(original)
                continue

            notice = self._notice_for(name, params, element_text, outcome.record)
            changed = True
            if notice is not None:
                kept.append(notice)
            # notice 为 None 时**什么都不放** —— 不能退化成放行。
            # run 继续、动作没执行，而"为什么没执行"在判定记录里查得到。

        # ── 3. 只在真的需要时才改写 ────────────────────────
        # ★★ S1 实测的坑：**无条件**改写会把 `done` 也一起改掉，
        #    于是 run 永远结束不了（LLM 每次想收工都被换成别的东西）。
        #    `changed` 标志就是为此存在的 —— 没有拒绝发生时，
        #    `model_output.action` 是**原对象**，一个字节都不动。
        if self.stop_reason is not None:
            # ★★ 硬停时把整个动作列表清空，而不是注入一个 `done(success=False)`。
            #
            #   为什么不是注入 done（我最初的写法）：`done` 的参数模型
            #   **取决于这次 run 有没有配 output_model**
            #   （`tools/service.py:2006-2013`：配了就是 `StructuredOutputAction[...]`，
            #   它要的是 `data`，不是 `success`/`text`）。
            #   注入一个写死 `{'success': False, 'text': ...}` 的 done，
            #   在有 output_model 的任务上会**校验失败** —— 而这恰恰发生在
            #   护栏最需要可靠的那一刻。硬停不能依赖一个会变的参数形状。
            #
            #   为什么清空是对的、而且不需要 done 来终结 run：
            #   硬停的停机是靠 `agent.register_should_stop_callback`（见 `should_stop`）。
            #   时序（`service.py:1200-1212`）：
            #       设 last_model_output → _check_stop_or_pause → 【我们的回调】→ _check_stop_or_pause
            #   第二次检查就在本回调返回之后，它会 `state.stopped = True` 并抛
            #   InterruptedError，被 `step()` 的 except 吞掉（`_handle_step_error`
            #   对 InterruptedError 只打一条 warning），于是**动作一个都不会执行**；
            #   `run()` 回到循环顶部看到 `state.stopped` 就 break，**正常返回 history**。
            #
            #   清空 `[]` 是**字面为真**的记录：这一步没有执行任何动作。
            #   留原动作的话，历史里会出现一个"看起来执行过"的危险动作 ——
            #   而它其实一个字节都没发出去。
            self.aborted_step = step_index
            model_output.action = []
            logger.warning("护栏硬停于 step=%s，本步动作已清空，run 将被终止", step_index)
        elif changed:
            model_output.action = kept

    # ── 单条判定 ──────────────────────────────────────────
    async def _evaluate_one(
        self,
        name: str,
        params: dict[str, Any],
        url: str,
        element_text: str | None,
        step_index: int,
    ) -> _OneDecision:
        # ★ `done` 豁免，且**显式记一条决定**而不是"不判、什么都不留"。
        #   理由：done 不改变页面状态（它只带 success/text/files_to_display），
        #   但它会**终结 run** —— 所以不能在它前面挂审批，否则每次正常结束
        #   都要人点一次，而人点的那个"批准"没有任何安全含义，只是噪音。
        #   （S1 的坑正是"改写 done → run 结束不了"，这里是同一个问题的正面表述。）
        #
        #   豁免也必须留痕：日志里"这一步的 done 是豁免的"和
        #   "这一步根本没判过"是两条完全不同的信息，不留就得靠人去数。
        if name == TERMINAL_ACTION:
            return _OneDecision(
                _record(EXEMPT_RULE_ID, "allow", "终止动作豁免：它不改变页面状态", element_text),
                approved=True,
            )

        result = self.policy.evaluate(name, params, url, element_text)

        # ── Layer 0 的补充：导航前预检目标 URL ─────────────
        # ★ 只对"库标记为会改变页面 **且** 带 url 参数"的动作做。
        #   `search` / `switch` / `go_back` 也在会改变页面那一类里，但它们没有 url 参数
        #   （参数是 query / tab_id / description），拿不到目标就没法预检。
        #   它们依赖 Layer 0（SecurityWatchdog 在真实导航时拦截）——
        #   **这是本层的一个诚实的边界，写进 docs/guardrail_design.md，不假装覆盖。**
        if result.is_allowed and name in self._page_changing:
            target = params.get("url")
            if isinstance(target, str) and target:
                nav = self.policy.check_navigation(target)
                if nav.is_blocked:
                    result = nav

        record = _record(
            result.rule_id, result.decision.value, result.reason, element_text,
            matched=result.matched_rule_ids,
        )

        if result.is_allowed:
            self.policy.record_success()
            return _OneDecision(record, approved=True)

        if result.is_blocked:
            tripped = self.policy.record_block()
            if tripped:
                self.stop_reason = (
                    f"连续 {self.policy.block_streak} 次被护栏拦下（上限 "
                    f"{self.policy.spec.max_consecutive_blocks}），判定为 LLM 在反复尝试"
                    f"同一个不该做的动作，主动终止 run。最后一条："
                    f"{name}「{element_text or ''}」被规则 {result.rule_id or 'default'} 拦下"
                )
                logger.warning("护栏硬停：%s", self.stop_reason)
            else:
                logger.warning(
                    "护栏拦截 step=%s 动作=%s 元素=%r 规则=%s 理由=%s",
                    step_index, name, element_text, result.rule_id, result.reason,
                )
            # ★★ 拦截事件从**这里**发，而不是在 runner 记完这一步之后从
            #    StepRecord 里反推。理由是那条路会漏掉最该看的一类：
            #
            #    runner 的 `_make_step_hook` 有一个"history 有没有变长"的闸门，
            #    硬停那一步**不产生历史项**，于是判定会被它显式丢弃
            #    （那个函数里有长注释解释为什么无处可挂）。
            #    而"连续被拦到上限于是整个 run 被终止"恰恰是看板上最该立刻反应的
            #    一件事 —— 从 step 记录反推的话，它永远显示不出来。
            #
            #    从判定点发则一条不漏：这里是所有 block 的唯一必经之路。
            self._publish(
                "guardrail_blocked",
                {
                    "step": step_index,
                    "action_name": name,
                    # ★ 脱敏：这条要推到浏览器上。判定记录那边落盘时另有脱敏，
                    #   两处都要做 —— 事件通道不经过 recorder。
                    "element_text": self.redactor.text(element_text) if element_text else None,
                    "url": self.redactor.text(url),
                    "rule_id": result.rule_id,
                    "reason": result.reason,
                    "matched_rule_ids": list(result.matched_rule_ids),
                    "block_streak": self.policy.block_streak,
                    # ★ 这一条决定看板要不要把整条时间线染红：它不是"又拦了一个动作"，
                    #   而是"run 到此为止"。两者在时间线上长得像，含义差一个量级。
                    "hard_stop": tripped,
                },
            )
            return _OneDecision(record, approved=False)

        # ── CONFIRM：问人 ─────────────────────────────────
        approved = await self._ask(name, params, url, element_text, result, step_index)
        record = record.model_copy(update={"approved": approved, "approved_by": self._last_approver})
        return _OneDecision(record, approved=approved)

    async def _ask(
        self,
        name: str,
        params: dict[str, Any],
        url: str,
        element_text: str | None,
        result: DecisionResult,
        step_index: int,
    ) -> bool:
        """问人。没有审批通道时 → **拒绝**（fail-closed）。"""
        if self.approver is None:
            self._last_approver = ""
            logger.warning(
                "需要人工确认的动作 %s 没有可用审批通道 → 按拒绝处理（fail-closed）", name
            )
            return False

        # ★ 构造前先脱敏：这份对象会被写进 pending/*.json、推到 Web 界面、
        #   落进 steps.jsonl，而 approver 无从知道哪些值是敏感的
        #   （approver.py:64-67 把这条写成了契约）。
        req = ApprovalRequest(
            run_id=self.run_id,
            step=step_index,
            action_name=name,
            params=self.redactor.obj(params),
            element_text=self.redactor.text(element_text) if element_text else None,
            url=self.redactor.text(url),
            rule_id=result.rule_id,
            reason=result.reason,
        )
        # ★★ approval_required 必须在 **await 之前**发出去，这条顺序是功能性的：
        #    看板是唯一能让人知道"有人在等审批"的地方，而 run 此刻正挂在这个
        #    await 上、什么都不做。先 await 再发的话，人永远等不到那张卡片 ——
        #    一个永远不弹的审批卡片，表现和"没有需要审批的动作"一模一样。
        #
        #    载荷直接用 req 的字段（它**已经在上面脱敏过了**，approver.py:64-67
        #    把"构造前先脱敏"写成了契约）。这里是从脱敏后的对象取值，
        #    不是另取一份原始参数 —— 后者会让浏览器上出现一份没洗过的数据。
        self._publish(
            "approval_required",
            {
                "approval_id": req.id,
                "step": req.step,
                "action_name": req.action_name,
                "params": req.params,
                "element_text": req.element_text,
                "url": req.url,
                "rule_id": req.rule_id,
                "reason": req.reason,
                "summary": req.summary(),
            },
        )
        out = await self.approver.request(req)
        self._last_approver = out.approved_by
        logger.info("审批 %s → %s（%s）", req.id, out.outcome.value, out.approved_by or "-")
        # ★ 用 `outcome.value` 而不是 `approved` 布尔：四种结局里只有一种意味着
        #   "护栏按预期工作"（人真的点了拒绝）。超时和通道故障都是布尔上的
        #   "没批准"，但它们说明**审批通道本身有问题** —— 这正是
        #   approver.py 里 ApprovalOutcome 那段注释坚持要分开的那件事。
        #   前端因此能画出三种不同的卡片而不是一句"未获批准"。
        self._publish(
            "approval_resolved",
            {
                "approval_id": req.id,
                "step": req.step,
                "action_name": req.action_name,
                "outcome": out.outcome.value,
                "approved": out.approved,
                "approved_by": out.approved_by,
                "decided_at": out.decided_at,
                "note": out.note,
            },
        )
        return out.approved

    # ── 造给 LLM 看的说明 ──────────────────────────────────
    def _notice_for(
        self,
        name: str,
        params: dict[str, Any],
        element_text: str | None,
        record: GuardrailDecisionRecord,
    ) -> Any | None:
        """把被拒的动作换成一条说明。造不出来返回 None（调用方按 fail-safe 处理）。

        ★ 措辞按【拒绝的来源】分三种，不合成一句：
            · 系统拦的（block）        → 说明命中了哪条规则；
            · 人拒的（confirm+denied）  → 带上审批人；
            · 没有人可问（无通道/超时）  → 明说"没有通道"，因为那不是"有人拒绝了"。
          合成一句的话，第三种会被读成第一种，于是"审批通道坏了"永远显示成
          "护栏正常工作" —— 而那正是最需要被发现的问题
          （同一个理由写在 approver.py 的 ApprovalOutcome 注释里）。
        """
        if record.approved is None:
            source = "系统自动拦下（未征询人工）"
        elif record.approved is False and record.approved_by:
            source = f"人工拒绝（审批人={record.approved_by}）"
        else:
            source = "未获批准（无审批通道应答或超时，按拒绝处理）"

        message = (
            f"HUMAN_DENIED: 动作 {name}「{element_text or ''}」被拒绝 —— {source}。"
            f"命中规则={record.rule_id or 'default'}，理由={record.reason}。"
            f"不要重复这个动作。请换一种方式完成目标，或直接调用 done 汇报受阻原因。"
        )
        try:
            return make_notice_action(self._action_model, message)
        except Exception:  # noqa: BLE001 —— 造不出来不是致命错误，但要留痕
            logger.exception("构造 %s 动作失败，本步将丢弃该动作而不是放行它", GUARD_NOTICE_ACTION)
            return None

    # ── 硬停：交给库自己的停机通道 ────────────────────────
    async def should_stop(self) -> bool:
        """给 `Agent(register_should_stop_callback=...)` 用。**无参数，但必须 async**。

        ★★★ 这个 `async` 不是风格选择，是库的硬要求，而且**写错了不会立刻报错**：
          `Agent.__init__` 的注解是（service.py:165）

              register_should_stop_callback: Callable[[], Awaitable[bool]] | None

          调用点是 `if await self.register_should_stop_callback():`（service.py:1018）。
          同步版本返回一个 `bool`，`await bool` 抛
          `TypeError: 'bool' object can't be awaited`。

          ⚠️ 为什么这个坑特别隐蔽 —— 它被包在 `_handle_step_error` 里：
            每一次 `_check_stop_or_pause()`（service.py:1109 / 1203 / 1209 / 2773 四处）
            都变成一个**步进错误**，而 `_check_stop_or_pause` 每步至少被调两次。
            所以表现是 `Result failed 1/6 times: 'bool' object can't be awaited`
            刷屏 → 连续失败到上限 → run 结束，**0 步、0 行数据**，
            而报错文本里完全看不出"是我们的回调"—— 它只说有个 bool 没法 await。

        为什么单独说"四个回调里只有这一个"：
          `register_new_step_callback`（service.py:164）和 `register_done_callback`
          的注解都显式写了 `Awaitable[X] | X` **两种都收**，
          只有本回调和 `register_external_agent_status_raise_error_callback`
          是纯 `Awaitable`。所以"前两个同步写也能跑"这件事，
          会把人骗到第三个上。

        ★ `verify_stop_callback()` 在开跑前 await 一次本方法 ——
          这类错误必须在启动期就死，而不是跑到第 6 秒才以一句无关报错的形式出现。
        ★ 为什么硬停不用"注入一个 done 动作"来实现：见 `on_new_step` 里的长注释
          （done 的参数模型随 output_model 变，会在最需要可靠的那一刻校验失败）。

        ★ 为什么用这个回调而不是自己抛异常：
          库在 `_check_stop_or_pause`（`service.py:1013-1021`）里已经实现了
          "置 `state.stopped` + 抛 InterruptedError"这套完整语义，而
          `step()` 的 except 对 InterruptedError 有专门分支（只打 warning，
          不当错误处理），`run()` 的循环顶部也会读 `state.stopped` 干净退出。
          自己抛异常要重新踩一遍这些分支，而它们是库的内部约定。

        ★ 时序是**同一步内**生效：这个回调在 `_handle_post_llm_processing`
          之后被调（`service.py:1212`），也就是本拦截器 on_new_step 返回之后，
          而 `_execute_actions` 还在更后面 —— 所以这一步的动作一个都不会执行。
        """
        return self.stop_reason is not None

    async def verify_stop_callback(self) -> None:
        """开跑前**真的 await 一次**本拦截器的停机回调。接线不对就当场死。

        ★★ 为什么是"真的 await"而不是查 `inspect.iscoroutinefunction`：
          查签名只能证明"它现在是 async"，证明不了"库会怎么调它"。
          而这次的 bug 恰恰是**我们的形状**和**库的调用方式**对不上 ——
          真正的契约是"库会 await 它"，那就 await 一次。

        ★ 为什么这个检查必须存在（它是这次 live 验收抓到的实锤）：
          同步版 `should_stop` 让每一次 `_check_stop_or_pause()` 都抛
          `TypeError: 'bool' object can't be awaited`，被 `_handle_step_error`
          当成步进错误吞掉。结果是：护栏的**硬停永远不生效**，
          同时 run 每步都失败、连续 5 次后停下，0 步 0 行，
          而报错文本指向的是一个 bool —— 没人会顺着它找到这里。

          ⚠️ 更坏的一层：硬停失灵是**静默**的。护栏拦得住单个动作（那条路径不经过
            这个回调），所以"护栏在工作"的假象成立；只有"连续被拦到上限要硬停"
            这一条路径是坏的 —— 而那正是最后一道保险。
        """
        outcome = self.should_stop()
        if not inspect.isawaitable(outcome):
            raise RuntimeError(
                f"停机回调必须是 async 的：`should_stop()` 返回了 "
                f"{type(outcome).__name__}，而库会 `await` 它的返回值"
                f"（agent/service.py:1018；注解见 service.py:165 的 "
                f"`Callable[[], Awaitable[bool]]`）。"
                f"同步版本的表现是每步都抛 'bool' object can't be awaited，"
                f"既让硬停失效、又让整个 run 一步都跑不完。"
            )
        value = await outcome
        if not isinstance(value, bool):
            raise RuntimeError(
                f"停机回调必须返回 bool，实际是 {type(value).__name__}。"
                f"库把它当条件用（service.py:1018），非 bool 的真值语义会让"
                f"停机时机变得不可预期。"
            )

    # ── 交接 ──────────────────────────────────────────────
    def bind_action_model(self, agent: Any) -> None:
        """从 agent 上取出 `ActionModel` 备用。

        ⚠️ 它是**当场取的那个对象**，而库会在换页时重建它
          （`_update_action_models_for_page`，`service.py:4024` / 调用点 1113）。
          这对我们是安全的，因为 `guard_notice` 没有配 `domains`
          （`create_action_model` 在给 URL 时会把它一直包含进来）。
          但**不要在别处缓存动作模型类然后跨页复用** —— 那个类是按页面的
          动作集合生成的。
        """
        self._action_model = agent.ActionModel

    def take_decisions(self) -> list[GuardrailDecisionRecord]:
        """取走本步的判定并清空。由 on_step_end 侧调用。"""
        out = self._pending_decisions
        self._pending_decisions = []
        return out

    def _publish(self, type_: str, data: dict[str, Any]) -> None:
        """往实时通道发一条。没有总线时**静默跳过**（不是错误）。

        ★ 这里**不 try/except**：`EventBus.publish` 本身已经被设计成不会失败
          （同步、不抛、满了就丢并通报），给它套一层 except 只会掩盖
          我们自己拼错的载荷。真要炸就炸在开发期。
        """
        if self.events is None:
            return
        self.events.publish(RunEvent(type=type_, run_id=self.run_id, data=data))

    async def aclose(self) -> None:
        closer = getattr(self.approver, "aclose", None)
        if closer is not None:
            await closer()


# ── 内部小类型 ────────────────────────────────────────────
class _OneDecision:
    """一条判定的结果 + 是否放行。★ 用一个类而不是 tuple：
    两个布尔相邻的位置传参（`(record, True)`）读代码时极容易看反。"""

    __slots__ = ("record", "approved")

    def __init__(self, record: GuardrailDecisionRecord, *, approved: bool) -> None:
        self.record = record
        self.approved = approved


def _record(
    rule_id: str | None,
    decision: str,
    reason: str,
    element_text: str | None,
    *,
    matched: Iterable[str] = (),
) -> GuardrailDecisionRecord:
    return GuardrailDecisionRecord(
        rule_id=rule_id,
        decision=decision,
        reason=reason,
        matched_rule_ids=tuple(matched),
        decided_at=now_iso(),
    )


def _iter_actions(model_output: Any) -> Iterable[tuple[Any, str, dict[str, Any]]]:
    """把 `model_output.action` 拆成 `(原对象, 动作名, 参数)` 三元组。

    ★ 动作名 = `exclude_unset=True` 之后的**第一个键**。这不是猜的 ——
      库自己在 multi_act 里就是这么取的：
      `action_name = next(iter(action.model_dump(exclude_unset=True).keys()))`
      （`service.py:2755-2756`）。
      必须用 exclude_unset：否则每个动作对象都会带上一堆未设置的默认字段，
      第一个键会变成一个没意义的字段名。
    """
    for act in getattr(model_output, "action", None) or []:
        try:
            dumped = act.model_dump(exclude_unset=True)
        except Exception:  # noqa: BLE001
            continue
        for name, params in dumped.items():
            yield act, str(name), params if isinstance(params, dict) else {}


def _action_indices(actions: Iterable[tuple[Any, str, dict[str, Any]]]) -> list[int]:
    """本步要取文本的元素索引。★ 只取动作真正指向的那些，
    不是整个 selector_map —— 后者是几百个元素，全取会让记录膨胀到读不动。"""
    out: list[int] = []
    for _, _, params in actions:
        idx = params.get("index")
        if idx is None:
            continue
        try:
            out.append(int(idx))
        except (TypeError, ValueError):
            continue
    return out


def _text_for(element_texts: dict[int, str], params: dict[str, Any]) -> str | None:
    """动作落在哪个元素上。★ 拿不到返回 None —— 而 None 会让
    `match_element_text` 判不命中（fail-safe 方向，见 rules.py:118-124）。"""
    idx = params.get("index")
    if idx is None:
        return None
    try:
        return element_texts.get(int(idx))
    except (TypeError, ValueError):
        return None
