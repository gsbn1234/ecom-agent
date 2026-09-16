"""护栏规则的数据模型。

纯数据 + 纯匹配，不碰浏览器、不碰 IO —— 这层的每一个判定都可以用一条单元测试钉死。
真正的执行（拦截、审批、改写动作）在 interceptor.py，那层才需要浏览器。
"""
from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ecom_agent.compat import match_url_pattern


class Decision(str, Enum):
    """一个动作的处置结论。

    ★ 枚举值的字符串顺序【不代表】严格程度，严格程度由 severity 显式定义。
      这是刻意的：如果靠枚举定义顺序或字典序来比大小，那"往中间插一个新等级"
      就会静默改变所有比较结果。显式 severity 让这件事不可能出错。
    """

    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    def __lt__(self, other: "Decision") -> bool:
        return self.severity < other.severity


_SEVERITY: dict[Decision, int] = {
    Decision.ALLOW: 0,
    Decision.CONFIRM: 1,
    Decision.BLOCK: 2,
}


class GuardrailRule(BaseModel):
    """一条规则：四个匹配条件【全部满足】才算命中（AND 语义）。

    四个条件都可以留空，留空 = 该维度不设限。全空 = 匹配一切动作。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    decision: Decision
    reason: str = ""

    # ── 四个匹配维度，AND 关系 ────────────────────────────
    match_action: list[str] | None = None
    """动作名精确匹配（如 ["click", "input"]）。None = 任意动作。"""

    match_url: str | None = None
    """域名/URL 通配。语义与 Layer 0 白名单【同源】，见 compat.match_url_pattern。"""

    match_element_text: str | None = None
    """对元素可读文本的正则，不区分大小写。None = 不看文本。"""

    match_param_regex: str | None = None
    """对 json.dumps(params) 的正则，不区分大小写。None = 不看参数。"""

    # ── 加载期校验 ────────────────────────────────────────
    @field_validator("match_element_text", "match_param_regex")
    @classmethod
    def _regex_must_compile(cls, v: str | None) -> str | None:
        """★ 正则合法性在【加载期】校验，不在运行期。

        理由很实际：运行期发现正则写错，是在浏览器已经导航过去、LLM 已经下了指令之后。
        那时错误表现为"某条规则莫名其妙没生效"，而根因（一个打错的括号）藏在 YAML 里，
        中间隔着好几层调用栈。加载期校验让这个错误在 `main.py run` 的第一秒就报出来，
        并且报错信息能直接指到那行 YAML。
        """
        if v is None:
            return None

        # ★ 空串必须单独拦掉，不能只靠 re.compile。
        #   re.compile("") 是【合法】的，而且匹配一切 —— 于是一条本想"只拦特定文本"
        #   的规则会变成"拦死所有操作"。方向完全反了：本意是缩小范围，实际是全体封锁。
        #   注意这不会以报错的形式出现，只会表现为"所有操作都要人工确认"，
        #   而人看到的现象是"护栏好像有点烦"，不是"配置错了"。
        #   要表达"任意非空文本"请写 '.+'，意图明确且看得见。
        if v == "":
            raise ValueError(
                "正则不能是空串：空正则会匹配一切，规则会从『只拦特定文本』变成『拦死所有操作』。"
                "若要匹配任意非空文本，请显式写 '.+'。"
            )

        try:
            re.compile(v, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"正则无法编译：{v!r} —— {e}") from e
        return v

    # ── 匹配 ──────────────────────────────────────────────
    def matches(
        self,
        action_name: str,
        params: dict[str, Any],
        url: str,
        element_text: str | None,
    ) -> bool:
        """四个维度 AND。任一维度不满足即整条不命中。"""
        if self.match_action is not None and action_name not in self.match_action:
            return False

        if self.match_url is not None and not match_url_pattern(url, self.match_url):
            return False

        if self.match_element_text is not None:
            # ★ 拿不到元素文本时【不命中】，而不是当作空串继续匹配。
            #   如果把 None 当空串，那么 `match_element_text: ""` 这类写错的规则会匹配一切 ——
            #   一个写错的规则从"不生效"变成"拦死所有操作"，方向完全反了。
            if element_text is None:
                return False
            if not re.search(self.match_element_text, element_text, re.IGNORECASE):
                return False

        if self.match_param_regex is not None:
            # ★ 序列化后再正则，而不是遍历 dict 的值分别匹配。
            #   真正的好处是：正则可以同时约束【键】和【值】的形态 ——
            #   比如 `"goods_id":\s*"\d{6,}"`（参数名叫 goods_id 且值是 6 位以上数字）。
            #   遍历各字段的值分别匹配是表达不出这个的：值本身没有名字。
            #
            #   ⚠️ 一个想当然的误解，已用对照实验钉死（test_guardrail_policy.py）：
            #      整体序列化【不能】检出"跨字段拼接出来的词"。
            #      json.dumps({"a":"sig","b":"nout"}) → '{"a": "sig", "b": "nout"}'，
            #      中间的 JSON 分隔符把 "signout" 打断了。别指望这个。
            #
            #   sort_keys=True 是为了确定性：不加的话 dict 按插入序输出，
            #   同一个逻辑动作、键序不同，匹配结果就可能不同 —— 那种不确定性
            #   在审计日志里是灾难（同一次操作两次 review 结论不一样）。
            #   default=str 兜住 Decimal/Path 这类不可 JSON 化的值，避免护栏自己抛异常。
            blob = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
            if not re.search(self.match_param_regex, blob, re.IGNORECASE):
                return False

        return True


class GuardrailSpec(BaseModel):
    """一份完整的护栏策略，直接对应 YAML 里的 guardrails: 段。"""

    model_config = ConfigDict(extra="forbid")

    allowed_domains: list[str] = Field(default_factory=list)
    prohibited_domains: list[str] = Field(default_factory=list)

    default_decision: Decision = Decision.CONFIRM
    """★ 默认是 confirm，不是 allow。

    LLM 的失败模式恰恰是【做了你没预料到的那个动作】。默认放行等于护栏只在
    写了规则的地方生效 —— 而"没写规则的地方"正是它最可能乱来的地方。
    默认 confirm 让未知动作先停下来问人，代价是偶尔多问一次，收益是漏网率大降。
    """

    approval_timeout_s: int = Field(default=300, gt=0)
    max_consecutive_blocks: int = Field(default=3, gt=0)
    """连续被拦多少次就硬停整个 run。

    ★ 为什么不用 browser-use 自己的 max_failures：那个指标把所有失败都算进去
      （网络抖动、元素没找到、LLM 解析错），跟我们关心的"被护栏拦了"不是一回事。
      护栏需要自己的计数器，否则一次网络抖动就能把"连续被拦"的额度耗掉。
    """

    rules: list[GuardrailRule] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        """★ 上游硬限制：allowed + prohibited 合计超过 100 条会被静默转成 set，
        并且【完全禁用通配符】（browser/profile.py:749-764）。

        这是最恶劣的一类失败：配置写得越多，护栏反而越弱 —— 而且没有任何警告，
        表现为"我明明写了 *.pinduoduo.com，怎么子域还是被拦"。

        ★ 必须用 model_post_init 而不是 field_validator：限制是【两个字段的合计】，
          逐字段校验在结构上就测不出真正的约束（各自 60 条、合计 120 条会被放过）。
        """
        combined = len(self.allowed_domains) + len(self.prohibited_domains)
        if combined > 100:
            raise ValueError(
                f"allowed+prohibited 合计 {combined} 条，超过上游 100 条硬限制："
                "超过后通配符会【静默失效】，护栏反而变弱。请拆分任务。"
            )

    @field_validator("rules")
    @classmethod
    def _rule_ids_must_be_unique(cls, v: list[GuardrailRule]) -> list[GuardrailRule]:
        """规则 id 必须唯一。

        ★ 不唯一时不会有人发现 —— 决策照样出得来，只是报告里记的 rule_id
          指向了另一条规则，事后追溯看到的原因是错的。审计信息错了比没有更糟。
        """
        seen: set[str] = set()
        dupes: set[str] = set()
        for r in v:
            if r.id in seen:
                dupes.add(r.id)
            seen.add(r.id)
        if dupes:
            raise ValueError(f"规则 id 重复：{sorted(dupes)}")
        return v
