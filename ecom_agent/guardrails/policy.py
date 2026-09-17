"""护栏策略引擎：把「一个动作该不该放行」变成一个纯函数。

★ 纯函数，零 IO、零浏览器、零 async。
  这不是洁癖 —— 护栏是安全组件，它的正确性必须能被【穷举式地】验证。
  一旦它依赖浏览器状态，"所有规则的组合行为"就没法用测试覆盖了，
  只能靠人工 review，而人工 review 恰恰最容易漏掉规则的交互。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ecom_agent.compat import match_url_pattern
from ecom_agent.guardrails.rules import Decision, GuardrailSpec


@dataclass(frozen=True)
class DecisionResult:
    """一次判定的完整结果。

    ★ 保留 matched_rule_ids（全部命中的规则），而不只有赢家。
      事后审计时"为什么是这个结论"往往取决于【还有哪些规则也命中了】——
      比如一条 allow 和一条 block 同时命中、最后判 block，
      只记赢家就看不出这里存在规则冲突。规则冲突是要人去改 YAML 的信号。
    """

    decision: Decision
    rule_id: str | None
    reason: str
    matched_rule_ids: tuple[str, ...] = ()

    @property
    def is_allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def needs_human(self) -> bool:
        return self.decision is Decision.CONFIRM

    @property
    def is_blocked(self) -> bool:
        return self.decision is Decision.BLOCK


@dataclass
class _BlockStreak:
    """连续被拦计数器（护栏自己的，不用 browser-use 的 max_failures）。"""

    count: int = 0
    limit: int = 3
    tripped: bool = False

    def record(self) -> bool:
        self.count += 1
        if self.count >= self.limit:
            self.tripped = True
        return self.tripped

    def reset(self) -> None:
        self.count = 0


class GuardrailPolicy:
    """规则集的求值器。

    ★ 无状态求值 + 显式计数器：evaluate() 是纯函数，唯一的状态是连续被拦计数，
      而它被单独拎出来，因为它是跨 step 的（一次 run 的所有 step 共享同一个计数器）。
      把这两件事混在一个对象里是很多护栏实现出 bug 的源头。
    """

    def __init__(self, spec: GuardrailSpec) -> None:
        self.spec = spec
        self._streak = _BlockStreak(limit=spec.max_consecutive_blocks)

    # ── 主判定 ────────────────────────────────────────────
    def evaluate(
        self,
        action_name: str,
        params: dict[str, Any] | None = None,
        url: str = "",
        element_text: str | None = None,
    ) -> DecisionResult:
        """判定一个动作。

        ★ 聚合规则是【最严优先】（block > confirm > allow），与 YAML 里的书写顺序无关。

          为什么不是"先匹配先赢"：YAML 是有顺序的，如果先匹配先赢，那么
          "把一条 allow 规则写在 block 规则前面"就是一个【静默的安全漏洞】——
          功能测试全过，review 的人从 YAML 上也看不出来，因为那两条规则各自都对。
          最严优先让规则集的语义与顺序解耦：review 时只需要看集合本身，
          不需要同时判断顺序，审错的概率大幅下降。
        """
        params = params or {}
        matched = [
            r for r in self.spec.rules if r.matches(action_name, params, url, element_text)
        ]

        if not matched:
            # 未命中任何规则 → 用 default_decision（默认 confirm，见 rules.py 的说明）
            return DecisionResult(
                decision=self.spec.default_decision,
                rule_id=None,
                reason=(
                    f"未命中任何规则，按 default_decision={self.spec.default_decision.value} 处理"
                ),
                matched_rule_ids=(),
            )

        # ★ 排序而不是 max()：需要"严重度降序，平局时 rule_id 升序"这两个方向相反的关键字。
        #   用 max(severity=...) 只能表达第一个方向，平局时取到的是 id 最大的那条 ——
        #   与意图相反，而且不报错。显式排序能一次说清两个方向。
        #   平局按 id 而非 YAML 顺序：否则"两条 block 规则调换位置"会让报告里的
        #   rule_id 变来变去，追溯时看着像行为变了，实际只是顺序变了。
        winner = sorted(matched, key=lambda r: (-r.decision.severity, r.id))[0]
        return DecisionResult(
            decision=winner.decision,
            rule_id=winner.id,
            reason=winner.reason or f"命中规则 {winner.id}",
            matched_rule_ids=tuple(sorted(r.id for r in matched)),
        )

    # ── 导航前置校验（Layer 0 的补充）──────────────────────
    def check_navigation(self, url: str) -> DecisionResult:
        """导航到某个 URL 之前的前置判定。

        ★ 为什么不依赖 SecurityWatchdog 就够了：
          它拦不住"站内页面里的 fetch()/XHR 请求到站外" —— 那不是导航事件。
          而且它失败时的表现是一个 ActionResult(error=...)，要到动作执行后才看得到。
          这里的前置校验让"要导航去哪"在动手之前就是可判定、可测试、可审计的。

        prohibited 优先于 allowed：两者都命中时判 block。
        这是刻意的 —— allowed 列表常常用宽通配（*.pinduoduo.com），
        而 prohibited 是精确的例外清单，例外必须能覆盖通配。
        """
        for pat in self.spec.prohibited_domains:
            if match_url_pattern(url, pat):
                return DecisionResult(
                    decision=Decision.BLOCK,
                    rule_id="prohibited-domain",
                    reason=f"URL 命中禁止域名 {pat!r}",
                    matched_rule_ids=("prohibited-domain",),
                )

        if self.spec.allowed_domains:
            for pat in self.spec.allowed_domains:
                if match_url_pattern(url, pat):
                    return DecisionResult(
                        decision=Decision.ALLOW,
                        rule_id="allowed-domain",
                        reason=f"URL 命中白名单 {pat!r}",
                        matched_rule_ids=("allowed-domain",),
                    )
            # ★ 配了白名单但没命中 → 一律拦，不看 default_decision。
            #   白名单的语义就是"只允许这些"，回落到 confirm 会让它变成"建议清单"。
            #   而浏览器导航是不可逆的：一旦过去，页面上的脚本已经执行了。
            return DecisionResult(
                decision=Decision.BLOCK,
                rule_id="not-in-allowlist",
                reason=f"URL 不在白名单内：{url}",
                matched_rule_ids=("not-in-allowlist",),
            )

        return DecisionResult(
            decision=self.spec.default_decision,
            rule_id=None,
            reason="未配置白名单，按 default_decision 处理",
        )

    # ── 连续被拦计数 ──────────────────────────────────────
    def record_block(self) -> bool:
        """记一次拦截。返回 True 表示已达上限，应当硬停整个 run。

        ★ 为什么需要它，而不是让它撞 browser-use 的 max_failures：
          那个指标把网络抖动、元素找不到、LLM 解析失败统统算进去。
          一次网络抖动就能吃掉"连续被拦"的额度，于是护栏会在没被撞的情况下提前硬停；
          反过来，护栏连撞三次也可能因为中间夹着几次成功而永远触发不了 max_failures。
          护栏需要自己的、语义精确的计数器。
        """
        return self._streak.record()

    def record_success(self) -> None:
        """记一次**放行**的动作 —— 连续计数归零。

        ★ 归零条件是"被放行"，不是"执行成功"。
          因为要数的是"LLM 连续撞护栏"这件事，而它执行成不成功是另一回事：
          LLM 可能在反复尝试同一个被拒的动作，每次都以不同方式失败。

        ★★ 这里原本写的是"未被拦截就归零"，**措辞与行为不符**（实测发现的）。
          准确的语义是：**计数器数的是"距上次放行以来的 BLOCK 次数"**，
          而 `confirm` 被人工拒绝的动作**既不计数也不归零** ——
          它走 `needs_human` 分支，`record_block` 和 `record_success` 都不会被调到。

          为什么不把"人工拒绝"也算进去：一次人工拒绝意味着**有人刚看过这一步**
          并做了决定，人是已经在环路里的，run 没有失控；
          而 BLOCK 是无人值守时自动发生的，连撞多次才说明 LLM 在空转。
          两者混进一个计数器，"连续被拦"就同时意味着两件不同的事，
          调 `max_consecutive_blocks` 时不知道该往哪边调。

        ⚠️ 所以报告里的硬停原因"连续 N 次被护栏拦下"指的是 **BLOCK**，
          不是"总共 N 次没通过"。拦截器里 `record_block` 只在这一处被调用，
          改那里的时候要一起改这句话。
        """
        self._streak.reset()

    @property
    def block_streak(self) -> int:
        return self._streak.count
