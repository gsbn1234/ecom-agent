"""任务 DSL 的数据模型 —— 一份 YAML 的结构定义。

★ 全字段 extra="forbid"。理由不是洁癖：
  一份写错字段名的 YAML，如果被静默忽略，表现是"任务跑起来了但没做我要的事"，
  而错误的根因（一个拼错的键）藏在 YAML 里，中间隔着 LLM 的行为。
  报错必须发生在加载期，且报错信息里要带上那个字段名。

★ 参数校验发生在【创建浏览器之前】。
  非法参数不该烧 token，更不该让浏览器点错按钮 —— 校验顺序本身就是安全设计。
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ecom_agent.guardrails.rules import GuardrailSpec

ScalarType = Literal["string", "integer", "number", "boolean", "enum"]

# 占位符形态：{名字}。名字用 \w（Python 的 re 对 str 默认按 unicode 匹配，所以中文参数名也能认）。
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class ParamSpec(BaseModel):
    """一个任务参数的声明。对应 YAML 的 params: 段。

    ★ 这里是"每个字段都有独立强制点"的第一个强制点：
      参数的类型和范围由 pydantic 强制，而不是靠提示词里写"limit 不要超过 200"。
    """

    model_config = ConfigDict(extra="forbid")

    type: ScalarType = "string"
    required: bool = False
    default: Any = None
    description: str = ""

    enum: list[str] | None = None
    ge: float | None = None
    le: float | None = None

    @model_validator(mode="after")
    def _check(self) -> "ParamSpec":
        if self.type == "enum":
            if not self.enum:
                raise ValueError("type=enum 必须提供 enum 列表")
            if self.default is not None and self.default not in self.enum:
                raise ValueError(f"default={self.default!r} 不在 enum {self.enum} 内")
        if self.required and self.default is not None:
            # ★ 不算错，但几乎总是笔误：声明必填又给默认值，调用方会以为可以不传。
            raise ValueError("required=True 与 default 同时出现，语义矛盾：请二选一")
        if self.ge is not None and self.le is not None and self.ge > self.le:
            raise ValueError(f"ge={self.ge} > le={self.le}")
        return self


class PaginationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "click_next", "scroll"] = "none"
    max_pages: int = Field(default=1, ge=1, le=50)
    """★ 上限防"翻页地狱"：LLM 在翻页类任务上极容易一直翻下去，
      每翻一页烧一次 token。

    ⚠️ **它是软闸门，不是硬闸** —— 这个字段唯一的去处是编译进 `task_text`
      （`compiler.py:200` 那句"最多翻 N 页"），运行期**没有任何计数器读它**。
      实测：`mock_shop_readonly.yaml` 写 `max_pages: 1`，
      run `20260918T034157+0000-c9413c` 照样翻到了第 2 页。

    ★ 不做成硬闸是一个**决定**，不是遗漏：它防的是**成本**，不是**安全**；
      而"翻了一页"在三种 mode 下没有统一定义（`click_next` 点按钮 / `scroll`
      滚动触发 / `none` 不翻），真站点上访问详情页同样会让 URL 变化 ——
      按 URL 计数会**误停**。**一个数不准的硬闸比一句诚实的话更危险**：
      它会让人以为这里有保护。兜底是 `agent.max_steps`（browser-use 强制）
      和 `stop_when` 的"翻到空页就停"。完整取舍见 ADR 3。"""

    next_selector_hint: str = ""
    """★ 语义提示，不是 CSS 选择器。
      写 "分页条最右侧的'下一页'" 而不是 ".next-page-btn" ——
      因为真实站点的 class 是构建产物 hash，会随发版变；而"最右侧的下一页"不会。
      这正是选 browser-use 而不是 Playwright 手写选择器的核心理由。"""

    stop_when: str = ""


class RetrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=5)
    step_max_failures: int = Field(default=3, ge=1, le=20)
    on_empty_result: Literal["retry", "accept", "fail"] = "accept"


class ObservabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screenshot: bool = True
    record_llm_io: bool = True
    redact_extra: list[str] = Field(default_factory=list)
    """★ 除了自动脱敏（cookie/手机号/邮箱等），还要额外盖掉的字面值。
      比如演示时用的测试店铺名 —— 它本身不是敏感信息，但没必要出现在公开仓库的
      截图和日志里。这是"由使用者补充业务相关的脱敏词表"的入口。"""


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_vision: bool = False
    """★ 默认 False，且显式拒绝 True。

    理由（ADR-2）：本项目用 DeepSeek 文本模型，它【没有视觉能力】。
    打开 use_vision 会让库把截图塞进消息里，模型收到的是一堆它无法理解的内容，
    表现为"模型像是没看见页面"——而根因是一个布尔开关。
    真要用视觉模型时，把这个 validator 一起改掉，而不是悄悄绕过去。"""

    max_actions_per_step: int = Field(default=2, ge=1, le=10)
    max_steps: int = Field(default=60, ge=1, le=500)
    max_failures: int = Field(default=5, ge=1, le=50)

    @field_validator("use_vision")
    @classmethod
    def _vision_must_stay_off(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "本项目使用无视觉能力的文本 LLM，use_vision 必须为 False。"
                "若要启用，需同时更换为视觉模型并更新 ADR-2 的边界说明。"
            )
        return v


class CardField(BaseModel):
    """卡片列表里的一个字段：**怎么从一张卡里认出它**。

    ★★ 这个类型是"字段判据写进任务定义"这条设计的载体。它住在 DSL 里而不是
      住在 action 里，因为它是**可 review、可 diff、可单测的任务定义**的一部分，
      而不是某个采集器的私有参数 —— 采集器只是执行它。

    ★ 为什么判据由这里给、而不是由 LLM 填 action 参数：
      让模型每次现写一遍正则，等于把"哪一行算价格"交回给它，而那正是本项目
      拒绝的事（价格/库存不该由模型转录，见 actions/extract_table.py 顶部）。
      判据走闭包进 action，于是模型能决定的只有"读第几组、读多少"。
      —— 与"护栏条款不由 LLM 说了算"是同一条纪律。

    ★ 为什么是正则而不是"第几行"：
      行号会随徽标/活动标签的增删而漂移，而漂移的表现是**静默取错**
      （把"热度 100"当成价格 —— 那是个长得完全合法的数）。
      正则要么匹配上、要么匹配不上；匹配不上的会累积成 missing 计数显示出来，
      失败因此是**看得见**的。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="字段名，会作为键出现在结果的行字典里")
    pattern: str = Field(
        min_length=1,
        description="对卡片文本**逐行**匹配的正则。有捕获组就取第 1 组，没有就取整个匹配。",
    )

    @field_validator("pattern")
    @classmethod
    def _pattern_must_compile(cls, v: str) -> str:
        """★ 正则编译不过必须在**加载期**报出来。

        放到运行期报的代价：浏览器已经起来、token 已经开始烧，而报错会变成
        "采集器执行失败" —— 它指向采集器，真正的错处却是 YAML 里的一个括号。
        这里挡下来，错误信息里直接带着那段正则。
        """
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"正则编译不过（{exc}）：{v!r}") from exc
        return v


class TaskSpec(BaseModel):
    """一份完整的任务模板。对应一个 YAML 文件。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    id: str = Field(min_length=1)
    name: str = ""
    site: str = ""
    requires_login: bool = False
    start_url: str

    goal: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)

    params: dict[str, ParamSpec] = Field(default_factory=dict)
    output_model: str
    output_model_version: int = 1

    pagination: PaginationSpec = Field(default_factory=PaginationSpec)
    retry: RetrySpec = Field(default_factory=RetrySpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)
    agent: AgentSpec = Field(default_factory=AgentSpec)
    guardrails: GuardrailSpec = Field(default_factory=GuardrailSpec)

    # ★ 卡片列表类页面的字段判据（见 CardField）。**空 = 本任务不用 extract_cards**，
    #   于是一份没有卡片列表的任务（绝大多数）完全不受这个字段影响。
    card_fields: list[CardField] = Field(default_factory=list)

    @model_validator(mode="after")
    def _card_fields_are_consistent(self) -> "TaskSpec":
        """两条：字段名不能重名；声明了判据就必须在步骤里指名用 extract_cards。

        ★★ 第二条挡的是一类静默失效：YAML 里郑重写了三行判据，而**没有任何一步
          指示 LLM 去用那个采集器**。于是模型大概率改用库内置的 `extract`
          —— 那是 LLM 中介的，价格会被它转录一遍，而产物里一切正常。
          那份 card_fields 就成了"躺在 YAML 里从来没生效过"的死配置。

          ⚠️ 动作名写进步骤是**同一条规矩的延续**：护栏规则的 match_action 里也得写
             动作名，而动作名 = 函数名（见 extract_table.py 的 EXTRACT_TABLE_ACTION）。
             这条校验不检查"步骤写得对不对"，只检查"那个名字出现过没有"。
        """
        names = [f.name for f in self.card_fields]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"card_fields 里字段名重复：{dup} —— 结果的行字典会互相覆盖")

        if names and not any("extract_cards" in s for s in self.steps):
            raise ValueError(
                f"声明了 card_fields（{names}）但没有任何一步提到 extract_cards。"
                "把动作名写进某一步（例如「用 extract_cards 读取卡片列表」）—— "
                "否则这份判据不会被用上，而 LLM 会改用库内置的 extract（那会转录价格）。"
            )
        return self

    @field_validator("schema_version")
    @classmethod
    def _supported_schema_version(cls, v: str) -> str:
        """★ DSL 自身也要版本化。

        没有这个字段的话，将来给 DSL 加一个必填字段，所有旧 YAML 会在
        加载期报"缺少字段" —— 而使用者不知道是"我写错了"还是"DSL 升级了"。
        有了版本号，报错可以说"这份是 schema 1 的模板，当前需要 2，改动点如下"。
        """
        if v != "1":
            raise ValueError(f"不支持的 schema_version={v!r}，当前只支持 '1'")
        return v

    @model_validator(mode="after")
    def _steps_should_not_be_empty_strings(self) -> "TaskSpec":
        empty = [i for i, s in enumerate(self.steps) if not s.strip()]
        if empty:
            raise ValueError(f"steps 第 {empty} 项是空字符串（编号从 0 起）")
        return self

    def templated_texts(self) -> list[tuple[str, str]]:
        """会做占位符替换的【人类可读文本】字段，返回 (字段路径, 文本)。

        ★★ 这个方法存在的唯一理由是【防止校验和替换漂移】。
          校验（下面的 _placeholders_must_reference_params）和替换
          （compiler._substitute）必须看同一份字段清单：一旦校验覆盖 A、替换覆盖 B，
          就会出现"校验放行但没被替换"（占位符原样发给 LLM）或反过来
          "替换了但没校验"（拼错的占位符静默变成一段乱码指令）两种静默失效。
          两边都从这一个方法取清单，就不可能出现这种漂移。
        """
        out: list[tuple[str, str]] = [("goal", self.goal)]
        out += [(f"steps[{i}]", s) for i, s in enumerate(self.steps)]
        if self.pagination.next_selector_hint:
            out.append(("pagination.next_selector_hint", self.pagination.next_selector_hint))
        if self.pagination.stop_when:
            out.append(("pagination.stop_when", self.pagination.stop_when))
        return out

    @model_validator(mode="after")
    def _placeholders_must_reference_params(self) -> "TaskSpec":
        """占位符必须指向一个【渲染时一定有值】的参数。

        ★ 为什么这条值得单独一个校验：拼错的占位符是【静默失效】——
          `{keywork}` 不会报错，它会原样变成下发给 LLM 的指令文本，
          模型看到的是"按关键词「{keywork}」搜索"。这个提示词语法上没毛病、
          语义上是乱的，而根因（一个拼错的字母）藏在 YAML 里，
          中间隔着 LLM 的行为 —— 和 extra="forbid" 要挡的是同一类事故。

        ★ 为什么要检查"一定有值"而不只是"存在"：
          非必填且无默认值的参数，_coerce_params 会把它填成 None，
          该占位符【同样】不会被替换 —— 失败形态和拼错一模一样。
          所以能出现在占位符里的参数，要么 required，要么有 default。

        ⚠️ 只扫 templated_texts() 里的字段，【不扫护栏正则】。
          护栏的 match_element_text 是正则，而正则里合法的量词恰好长得像占位符：
          `试用{2}` 的 `{2}` 完全符合 PLACEHOLDER_RE。若把护栏文本也纳入扫描，
          一条完全正确的正则规则会被判成"未知占位符"。
          这就是上面那份清单必须收窄、而不是"扫全 spec 更保险"的原因。
        """
        problems: list[str] = []
        for where, text in self.templated_texts():
            for name in dict.fromkeys(PLACEHOLDER_RE.findall(text)):
                if name not in self.params:
                    hint = difflib.get_close_matches(name, list(self.params), n=1, cutoff=0.6)
                    tail = f"（是不是想写 {hint[0]!r}？）" if hint else ""
                    declared = sorted(self.params) or "（本任务没有声明任何参数）"
                    problems.append(f"{where}: 占位符 {{{name}}} 未声明{tail}；已声明：{declared}")
                else:
                    ps = self.params[name]
                    if not ps.required and ps.default is None:
                        problems.append(
                            f"{where}: 占位符 {{{name}}} 引用的参数既非必填也无默认值，"
                            f"渲染时它会是 None，占位符将原样留在提示词里。"
                            f"请把 {name} 标成 required 或给它一个 default。"
                        )
        if problems:
            raise ValueError("占位符检查未通过：\n" + "\n".join(f"  - {p}" for p in problems))
        return self

    def required_params(self) -> list[str]:
        return [k for k, v in self.params.items() if v.required and v.default is None]
