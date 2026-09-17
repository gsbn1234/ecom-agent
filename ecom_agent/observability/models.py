"""可观测性的数据模型 —— `run.json` 与 `steps.jsonl` 的 schema。

★ 为什么这些模型也用 pydantic 而不是直接 dict：
  它们是**长期资产**（审计、回放、报告、入库都读它）。dict 的问题是
  少一个键、多一个键、类型变了都不会有人告诉 —— 直到某天报告里少了一列。
  用 pydantic 的话，schema 漂移在写入那一刻就炸，而不是在读的时候。

★ 为什么 run.json 里要存【完整下发给 LLM 的文本】（compiled_task_text）：
  回放时最常见的问题是"当时模型到底看到了什么"。没有它，
  只能靠"任务 + 参数"去反推，而拼装逻辑一旦改过，反推出来的就不是当时那份。
  存档的是"字节本身"，不是"生成它的配方"。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ecom_agent.runtime.loginstate import NOT_PROBED
"""★ 这一行看着像"下层 import 上层"，其实不是：`loginstate` 是**叶子模块** ——
  它只有 asyncio / logging / dataclasses，碰不到 browser_use，也不碰 IO。
  （`runtime/__init__.py` 里那句"只有 browser.py 和 runner.py import browser_use"
  仍然成立，这里 import 的只是一段纯判定逻辑。）

★ 为什么宁可跨目录也要 import 这个常量，而不是在这里写个字面量 `""`：
  登录态的取值集合是**一处定义**的东西。在这儿再写一遍 `""`，就又多了一个
  "同一个真相存在两个地方"的口子 —— 而那正是这个字段本身要消灭的那类缺陷
  （某个通道从没发过它 / 两个地方各写各的）。"""


def now_iso() -> str:
    """统一的时间戳格式（UTC，秒精度）。

    ★ 用 UTC 而不是本地时间：CI 在 UTC、开发机在 UTC+8，
      混着存的话同一份 steps.jsonl 里的顺序看着会像倒流的。
      本地时间只在报告渲染时按需转换。
    """
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# 用常量而不是 Enum：这些值要直接进 JSON 和 SQLite 的 TEXT 列，
# 用 str 常量省掉一层 .value，也避免"忘了 .value 于是存成 'RunStatus.OK'"这种事故。
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
"""跑完了但结果不可用（解析失败、步数用尽、LLM 报错）。"""
BLOCKED = "blocked"
"""被护栏拦停。★ 与 FAILED 分开，因为处置方式完全不同：BLOCKED 不重试。"""


class LlmUsage(BaseModel):
    """一次 run 的 LLM 调用账。

    ★★ 为什么必须记【总调用次数】而不是"步数"：
      一个 run 里 LLM 并不只被调用 N 次（N = 步数）。已知的额外调用有两类：
        · **judge**：每次 run 结束后库会额外发一次评估调用（agent/service.py:1620），
          所以基线是 N+1；
        · **extract**：`extract` 动作内部会调 page_extraction_llm
          （tools/service.py:1197、1276），每调用一次就是一次额外的外部请求。
      只记步数会把成本算少，而"成本算少"不会报错，只会让预算判断一直偏乐观。
      这是 S4 探路时顺带发现的（见 docs/spikes.md）。

    ★★ 所以 `total_calls` 是【观测值】，不是分项之和 —— 这个区别是本类的重点。

      它由 runtime/llm.py 的 CountingLLM 在每次 `ainvoke` 时 +1 得到，是**直接数出来的**。
      分项（step / judge / extract / other）则是在 run 结束后*解释*出来的：
      前两类靠"库自己用的输出类型叫什么"，第三类靠数 steps.jsonl 里的 extract 动作。

      为什么不让 total 等于分项之和（一个 property 就够了）：
        那样"解释错了"会**自动**表现为"总数也错了"，两者一起漂移，没有任何一处对不上 ——
        而"分项解释错了但总数是对的"恰恰是最需要看出来的情况
        （它意味着我们对该库的调用模型已经过期）。
        现在 total 独立于解释，`note()` 会把两者的差额显式打出来。

      已知的、不去追的那条漏网：如果把 `fallback_llm` 配上了，走它发的请求
      不会经过我们包的那个对象。本项目的 config 不配 fallback_llm，
      所以这条不适用 —— 但**记在这里**，因为将来有人加上它时会静默少算。
    """

    model_config = ConfigDict(extra="forbid")

    total_calls: int = 0
    """★ 观测到的总调用次数（CountingLLM 直接数出来的，权威值）。"""

    by_format: dict[str, int] = Field(default_factory=dict)
    """★ {输出类型的名字: 调用次数}，同样来自观测。

      键是 `output_format` 的 `__name__`，没有 output_format 的记成 "none"。

      ★ 为什么不直接记成 step/judge 而要先记名字：
        因为 `agent.AgentOutput` 是**运行时动态造出来**的类型
        （service.py:786-790 `AgentOutput.type_with_custom_actions(...)`），
        名字不能写死在代码里 —— 写死的话库改一次命名，分类就静默全错。
        先原样记下库报出来的名字，解释交给 run 结束后（那时 agent 已经存在，
        可以直接问它 `agent.AgentOutput` 叫什么）。先观测、后解释。
    """

    step_calls: int = 0
    judge_calls: int = 0
    extract_calls: int = 0
    other_calls: int = 0
    """以上四项是【解释值】。other 定义为残差：total - step - judge - extract。

    ★★ `other_calls > 0` **就是**"我们对该库的调用模型已经过期"的那个信号 ——
      这是本类里最容易看错的一处，值得写清楚：

      因为 other 是残差，所以（在 residual ≥ 0 时）四项之和**恒等于** total，
      于是 `unexplained()` 恒为 0。也就是说**不能用 unexplained() 来发现
      "多出来一类我们没识别的调用"** —— 多出来的那部分被 other 吸收掉了，
      账反而是"平"的。

      能发现的只有反方向（residual < 0，解释出的比观测到的多）。
      所以看账的时候要盯的是 `other_calls` 这个数字本身：它非 0 就说明
      有调用没被归到 step/judge/extract 里 —— 可能是历史压缩、
      可能是库新加的一次内部请求。summary() 会把它标出来。
    """

    failed_calls: int = 0
    """★ 抛异常的调用次数 —— 它**计入** total_calls。

      理由：一次失败的请求也可能已经被计费（尤其超时和 5xx），
      而"成本账"宁可略微高估，不可低估 —— 低估不会报错，只会让预算判断一直偏乐观。

      它同时是 与库账本 对不上的主要原因：库只在拿到 `result.usage` 时才记一条
      （tokens/service.py:377），失败的调用它一条都不会记。
      所以对账时先看这个数：`total - failed` 才是"成功且有 usage"的期望上限。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    library_calls: int | None = None
    """库自己的令牌账本长度（`agent.token_cost_service.usage_history`）。

    ★ 这是本项目计数的**独立对照**，见 compat.observed_llm_calls 的说明。
      None 表示这个库版本没有可用的账本 —— **显式缺席，不是 0**。
      写成 0 会被读成"库说是零次调用"，那是一个完全不同的结论。
    """

    @property
    def explained_calls(self) -> int:
        return self.step_calls + self.judge_calls + self.extract_calls + self.other_calls

    def unexplained(self) -> int:
        """观测总数 与 解释之和 的差额。非 0 = 我们对该库的调用模型过期了。"""
        return self.total_calls - self.explained_calls

    def summary(self) -> str:
        parts = (
            f"LLM 调用 {self.total_calls} 次"
            f"（步进 {self.step_calls} / 裁判 {self.judge_calls} / 抽取 {self.extract_calls}"
            f" / 其他 {self.other_calls}）"
        )
        gap = self.unexplained()
        if gap > 0:
            # ★ 防御性分支：按 interpret_llm_usage 的算法，other 吸收了正残差，
            #   所以正的差额在当前代码路径上【到不了这里】（见 other_calls 的说明）。
            #   留着它是因为它守着的是"分项之和大于观测总数"这个方向 ——
            #   真要出现，只可能是有人手工构造了这本账，那更该被看见。
            #   ⚠️ 别把它当成主要信号：多出来一类调用时，那个数字在 other_calls 上。
            parts += f" ⚠️ 有 {gap} 次未被解释"
        elif gap < 0:
            # ★★ 负值也要说，而且要说【不一样的话】。
            #   含义和正值完全不同：观测到的调用次数**少于**我们解释出来的次数，
            #   也就是"我们数多了"。已见过的成因：一个 extract 动作在真正发请求之前
            #   就失败了（动作记在 steps.jsonl 里，调用没发生）。
            #   写成"有 -1 次未被解释"会读成一个可以忽略的小毛病，
            #   而它实际的含义是"我们对这个库的调用模型在某条路径上是错的"。
            parts += f" ⚠️ 解释出的调用比观测到的多 {-gap} 次（数多了，不是数漏了）"
        elif self.other_calls:
            # ★★ 这一条才是"我们对该库的调用模型过期了"的**主要**信号（理由见
            #   other_calls 的说明）：残差被 other 吸收之后账是平的，
            #   只有这个数字本身还记得"有 N 次归不了类"。
            #   打出来而不只是记在字段里 —— 字段会被报告和 CLI 原样展示，
            #   但没有任何一处会告诉你"这个非 0 值意味着什么"。
            parts += f" ⚠️ 其中 {self.other_calls} 次无法归类（步进/裁判/抽取都不是）"
        return parts



class ActionRecord(BaseModel):
    """一次动作的审计记录。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
    element_text: str | None = None
    """动作落在哪个元素上（人话）。

    ★ None 是一个【有意义】的值，不是缺失：说明取不到元素文本。
      护栏的 match_element_text 在拿到 None 时判【不命中】（见 rules.matches），
      所以记录里出现一堆 None 就是在提示"有一批规则在这段页面上是空转的"。
    """


class ResultRecord(BaseModel):
    """一次动作的执行结果。"""

    model_config = ConfigDict(extra="forbid")

    is_done: bool = False
    success: bool | None = None
    error: str | None = None
    extracted_preview: str | None = None
    """提取内容的前若干字符。★ 只是预览 —— 完整内容在 result.json / 库里，
    日志里塞全文会让步进记录膨胀到读不动。"""


class GuardrailDecisionRecord(BaseModel):
    """一次护栏判定。

    ★ 保留 matched_rule_ids（全部命中）而不只是赢家：
      "一条 allow 和一条 block 同时命中"是规则集本身需要人去改的信号，
      只记赢家就永久看不到它。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str | None = None
    decision: str
    reason: str = ""
    matched_rule_ids: tuple[str, ...] = ()
    approved: bool | None = None
    approved_by: str = ""
    decided_at: str = ""


class BrowserSnapshot(BaseModel):
    """在回调里【当场】从 browser_state 取走的那几个值。

    ★★★ 这个类存在的唯一理由是那条硬约束（S2-4 实测）：
      `browser_state.dom_state.selector_map` 与 BrowserSession 的内部缓存
      `_cached_selector_map` 是**同一个 dict 对象**（session.py:2494 直接赋值、没有拷贝），
      而 `reset()` 对它调 `.clear()` —— **原地清空**（session.py:664）。
      `run()` 在 keep_alive 为假时自己就会 reset。

      → **默认配置下，run() 一返回，所有存下来的 browser_state 快照都已经是空的。**

      失败形态是**静默的**：拿一个空 selector_map 去判护栏 → 没有任何规则命中 →
      而"没有命中"是一个**合法结果** → 不报错、不告警、测试全绿。
      **护栏看起来在跑，实际一步都没判。**

    ★ 所以这里的字段全是【值】，没有一个是指向库对象的引用。
      取值的动作必须在回调里完成（`recorder.snapshot_browser_state()`），
      之后任何人都拿不到库对象 —— 想"晚点再读"在类型上就做不到。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    title: str = ""
    element_texts: dict[int, str] = Field(default_factory=dict)
    """{元素索引: 可读文本} —— 同样是在回调里当场取的。"""

    has_screenshot: bool = False

    selector_map_size: int = 0
    """★ 当场 `len(selector_map)`。这个数字是 S2-4 那个静默失效的**探测器**。

      `element_texts` 为空有两种完全不同的原因：
        · 页面本来就没有这些元素（正常）；
        · 库已经把 map 原地清空了（`reset()` → `.clear()`，见上面的类注释）。
      两种在记录里长得**一模一样** —— 都是 `element_texts: {}`。
      加上这个数字之后，`selector_map_size=0` 才能把第二种指出来。

      ⚠️ 和 `same_frame_as_previous` 同一条纪律：它只是一个**原始信号**，不是结论。
        `selector_map_size=0` 也可能就是页面真没元素。记事实，判断留给看报告的人。
    """


class StepRecord(BaseModel):
    """`steps.jsonl` 的一行。

    ★ 为什么是 JSONL（每步一行）而不是一个大 JSON 数组：
      追加写、崩溃不丢已写的部分、单步可读（`tail -1` 就能看最后一步在干嘛）。
      一个大数组要写完才能解析，而长 run 恰恰是最可能崩的那种。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    attempt: int = 1
    step: int
    started_at: str = Field(default_factory=now_iso)
    duration_s: float = 0.0

    url: str = ""
    title: str = ""

    actions: list[ActionRecord] = Field(default_factory=list)
    results: list[ResultRecord] = Field(default_factory=list)
    guardrail_decisions: list[GuardrailDecisionRecord] = Field(default_factory=list)

    model_thought: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    screenshot_path: str | None = None
    screenshot_sha256: str | None = None

    same_frame_as_previous: bool = False
    """本步截图与上一步**逐字节相同**。

    ★★ 这是一个【原始信号】，不是结论 —— 这是 S5 踩出来的：
      实测里某一步的 sha 与上一步完全相同，而那一步的动作是 `scroll`，
      日志明写 "Scrolled down 1080px"，它**成功执行了**，
      只是那个页面比视口还短，滚不动，画面自然没变。
      同一句话用在 `click` 上就值得怀疑。

      所以字段名是"画面没变"这个事实本身，**不是** "点击无效"这个判断。
      报告里必须把它和动作名**并排**显示 —— 否则读者会把正常行为当故障查。
    """

    snapshot_missing: bool = False
    """这一步没有拿到回调快照。

    ★ 记成字段而不是打条日志：日志会被淹没，而字段会进 run.json 的统计。
      "有几步没快照"是一个能判断记录器是否完整工作的数字。
    """

    actions_not_executed: bool = False
    """★ 这一步的**动作一个都没执行**（护栏硬停中止了它）。

    ★★ 为什么必须显式标出来，而不是靠读者"看动作名是 guard_notice 就懂了"：

      被硬停中止的那一步**照样会被记录**：`_execute_step` 在 `self.step()`
      返回后无条件调 `on_step_end`（`service.py:2481-2482`），而
      InterruptedError 被 `step()` 自己的 `_handle_step_error` 吞掉
      （`service.py:1080-1082`），所以"被中止"这件事在那条记录里**没有任何痕迹**。

      ⚠️ 更糟的是这一行的 `results` 是**上一步的残留**：
        `_finalize` 用的是 `state.last_result`（`service.py:1356-1385`），
        而被中止的这一步 `_execute_actions` 根本没跑，`last_result` 没被更新。
      → 于是盘上会出现一行"动作是 guard_notice、结果却来自上一步"的记录。
        它看起来完全正常（有动作、有结果、有截图），**只是对不上**。

      这个字段就是那一行里唯一的破绽。看到它就该知道：
      本行的 `results` 不可信，去上一步看。
    """

    snapshot_overwritten: bool = False
    """上一步的快照还没被消费，就被这一步的新快照覆盖了。

    ★ 这是回调与钩子【配对错位】的探测器。正常情况下两者一一交替；
      一旦出现覆盖，说明时序和假设不符，那些步的 URL/截图会整体错位一帧 ——
      而错位的记录看起来完全正常（有 URL、有图、有动作），只是对不上。
      宁可多一个计数字段，也不要一个"看起来没问题"的错帧报告。
    """

    redacted: bool = True
    """★ 恒定 True，且是刻意写死的字段。

      它的作用不是"告诉你洗过了"，而是**让"忘了洗"变成一个显式的值** ——
      任何一条记录如果哪天被写成 redacted=False，报告里会直接显示出来。
      没有这个字段的话，未脱敏的记录和已脱敏的记录长得一模一样。
    """


class RunRecord(BaseModel):
    """`run.json` —— 一次 run 的头部事实。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    task_name: str = ""
    attempt: int = 1
    status: str = RUNNING

    started_at: str = Field(default_factory=now_iso)
    finished_at: str = ""
    duration_s: float = 0.0

    # ── 可复现追溯 ────────────────────────────────────────
    task_fingerprint: str = ""
    browser_use_version: str = ""
    model: str = ""
    provider: str = ""

    params: dict[str, Any] = Field(default_factory=dict)
    start_url: str = ""

    compiled_task_text: str = ""
    """★ 完整存下"下发给 LLM 的字节"。回放时能回答"LLM 到底看到了什么"。"""

    guardrail_policy: dict[str, Any] = Field(default_factory=dict)
    """护栏策略快照。

    ★ 必须存，且必须和 task_fingerprint 一起存：
      指纹相同而策略不同是最危险的情况 —— 回放时会用**当前**策略去解释一次
      按**旧**策略执行的记录，于是报告里"当时为什么放行"显示的是错的。
    """

    # ── 结果 ──────────────────────────────────────────────
    steps: int = 0
    llm: LlmUsage = Field(default_factory=LlmUsage)

    parse_status: str = ""
    rows_collected: int = 0
    sanity_flags: dict[str, list[str]] = Field(default_factory=dict)
    """{goods_id: [可疑点]}。★ 只标记不删除 —— 删掉就再也看不到 LLM 出错的方式。"""

    result_raw: str = ""
    """LLM 产出的原始 JSON。quarantine 时 products 表零行，但这份原文留着。"""

    # ── 这次 run 实际看到的登录态 ──────────────────────────
    login_state: str = NOT_PROBED
    """★★ 导航到任务起点之后**实测**到的登录态：`logged_in` / `login_page` /
    `unknown` / `""`（压根没探）。取值定义在 `runtime/loginstate.py`。

    ★ 为什么这个字段必须存在（Phase 6 真站点首跑撞出来的，不是推演）：
      一次零行的 run 可能是「店里本来就没数据」，也可能是「登录态失效、agent
      落在登录页上，于是按任务文本规规矩矩地停下汇报」。这两件事在**其他所有
      产物里长得一模一样** —— `status=completed`、`parse_status=empty`、
      退出码 0、报告齐全、截图也有。当时唯一能分辨它们的，是读 LLM 写的那句
      中文 note，或者人肉去看截图 —— 也就是说结论靠模型的措辞，不靠机制。
      换个措辞、或者换个不爱写 note 的模型，就又回到"看不出为什么"。

    ★ 记的是**观察到的**事实，不是**配置的**事实 —— 这个区别是全部意义所在。
      cookie 过期时，配置里照样写着那个 profile 路径，而 run 照样停在登录页。
      所以"记下配了哪个 profile"回答不了"这次到底登进去没有"。

    ⚠️ `""` 与 `"unknown"` **必须分开**：前者是"这个任务不需要登录态"（一切正常），
      后者是"探了但看不清"（需要人去看一眼）。两种沉默的处置完全相反。
    """

    login_state_reason: str = ""
    """判定依据（命中了哪些词、看的是哪个 URL）。

    ★ 要**原样给人看**：判错的时候，它是唯一能让人快速分清"是脚本看错了"
      还是"真的没登录"的线索。所以它自己就得能解释结论。
    """

    login_state_url: str = ""
    """探测时**实际落到**的那个 URL —— 跳转、重定向之后的那一个。

    ★ 和配置里的 `start_url` 不是一回事：两者不同恰恰说明发生了跳转，
      而那正是"落在登录页"最常见的形态。
    """

    # ── 观测自身的健康度 ──────────────────────────────────
    screenshot_count: int = 0
    same_frame_steps: list[int] = Field(default_factory=list)
    snapshot_missing_steps: list[int] = Field(default_factory=list)
    snapshot_overwrites: int = 0
    empty_selector_map_steps: list[int] = Field(default_factory=list)
    """★★ selector_map 为空（`len == 0`）的那些步 —— S2-4 那个静默失效的探测器。

    ★ 为什么这个字段非有不可，而不是"记在日志里就行"：
      库把 `selector_map` 原地清空时，护栏拿到的元素文本是空的 →
      `match_element_text` 一条都不命中 → 而"没命中"落到 `default_decision`，
      是一个**合法结果** → 不报错、不告警、测试全绿。
      **护栏看起来在跑，实际一步都没判。** 这个数字是唯一能戳破它的东西。

    ⚠️ 和 `same_frame_steps` 一样，它是**原始信号不是结论**：
      `selector_map` 为空也可能是页面真的没有元素。
      报告里要写成"这些步没拿到元素（可能是空页面，也可能是观测失效）"，
      不要写成"观测失效了"。
    """

    redaction_counts: dict[str, int] = Field(default_factory=dict)
    unsafe_auto_approved: bool = False
    """★ 调试后门（AutoApproveApprover）用过的痕迹。

      没有这个字段的话，"我调试时关掉审批跑过一次"和"这次真的没人需要审批"
      在记录里长得一样 —— 而前者是必须在真实场景里被发现的。
    """

    errors: list[str] = Field(default_factory=list)

    artifacts: dict[str, str] = Field(default_factory=dict)
    """{产物名: 相对 run 目录的路径}，报告和 Web 层据此找文件。"""
