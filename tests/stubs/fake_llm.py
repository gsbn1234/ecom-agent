"""FakeLLM —— 鸭子类型的假 LLM，用来驱动【真浏览器】跑确定性的 run。

★ 为什么需要它，以及为什么它是这个项目最重要的测试资产：
  护栏、拦截器、可观测、e2e 全都挂在 agent 的步进循环上。用一个真 LLM 测它们
  意味着：每次跑都花钱、结果不确定、失败原因可能是"模型今天不听话"。
  换成脚本化的假 LLM 之后，这些测试变成【确定性】的 ——
  可以精确断言"第 3 步被拦了之后，第 4 步 LLM 收到的消息里有没有 HUMAN_DENIED"。

★ 它不继承任何东西（连 BaseChatModel 都不继承）。
  依据（事实 18）：BaseChatModel 是 @runtime_checkable Protocol，
  且它的 __get_pydantic_core_schema__ 返回 any_schema()；
  Agent 里唯一的 llm 类型判断是 `isinstance(self.llm, ChatAnthropic)`。
  所以纯鸭子类型就够 —— 而且更硬：它不依赖库的内部基类，
  库改基类不会让这个桩失效（改契约才会，那时我们本来就该知道）。

★ 它编码的是【库的调用契约】，所以这里是"第二处脆弱依赖"（第一处是 compat.py）：
      response = await self.llm.ainvoke(messages, output_format=AgentOutput, session_id=...)
      parsed = response.completion          # 必须已经是 AgentOutput 实例，不是字符串
  契约来自 agent/service.py:1938-1958。改版时这里会红，
  而它红的位置正好告诉你"LLM 调用契约变了"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from browser_use.agent.views import AgentOutput, JudgementResult
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

# 脚本里的一步：一个动作（dict）或一批动作（list[dict]），
# 也可以是 callable(messages, step_index) -> 上面两者之一。
ActionSpec = dict[str, dict[str, Any]]
ScriptItem = ActionSpec | list[ActionSpec] | Callable[[list[Any], int], ActionSpec | list[ActionSpec]]


@dataclass
class LLMCall:
    """一次 ainvoke 的完整记录 —— 断言"LLM 到底看到了什么"靠它。"""

    step_index: int
    messages: list[Any]
    kwargs: dict[str, Any]

    def text(self) -> str:
        """把这次请求里所有文本拼起来（用于 substring 断言）。"""
        return "\n".join(_text_of(m) for m in self.messages)

    def image_part_count(self) -> int:
        """这次请求里带了几张图。

        ★ use_vision=False 时它必须恒为 0（ADR-2 的可执行证据）。
          截图在 use_vision=False 下照样会被采集（事实 10），所以"采集了截图"
          不能推出"把截图发给了模型" —— 这两件事必须分开验。
        """
        return sum(_image_parts_of(m) for m in self.messages)


@dataclass
class FakeLLM:
    """按脚本回答的假 LLM。

    用法（脚本里的动作名和参数就是 library 的动作名，不做任何翻译）：

        llm = FakeLLM(script=[
            {"navigate": {"url": "https://example.com"}},
            {"click": {"index": 3}},
            {"done": {"text": "完成", "success": True}},
        ])
        agent = Agent(task="...", llm=llm, browser=session)
        llm.bind(agent)          # ★ 必须调一次：拿 registry 才能造 action
        await agent.run()

    ★ 为什么 bind 是必须的而不是构造参数：
      ActionModel 是由 registry 在 Agent.__init__ 里动态创建的
      （agent/service.py:783），所以 llm 构造时那个类还不存在。
      与其猜形状，不如从 registry 自己取 —— 这样库加/改动作时桩自动跟上。
    """

    script: list[ScriptItem] = field(default_factory=list)
    model: str = "fake-scripted-v1"
    provider: str = "fake"
    #: 类属性而非实例属性 —— Agent 用 getattr 读它来决定是否跳过 API key 校验
    #: （agent/service.py:3970）。写成类属性可以省掉一次 setattr，
    #: 也让它对"先构造 Agent 再 bind"的顺序不敏感。
    _verified_api_keys: bool = True

    # ── 由 bind() 填充 ──
    _registry: Any = None

    # ── 运行记录 ──
    calls: list[LLMCall] = field(default_factory=list)
    """★ 只记【步进调用】（output_format 是 AgentOutput 的那些）。

    脚本游标走这个列表，所以它必须只被步进调用推进。
    judge / 抽取这类调用记在 other_calls 里 —— 混在一起会让脚本错位，
    而且那种错位表现为"第 3 步的动作和第 3 条脚本对不上"，极难排查。
    """

    other_calls: list[LLMCall] = field(default_factory=list)
    """非步进的 LLM 调用（judge、页面抽取等）。

    ★ 单独记它们本身就是一份证据：库在每次 run 结束后会额外发起一次 judge 调用
      （agent/service.py:1620，output_format=JudgementResult）。
      不知道这件事的人会以为"一个 run = N 次 LLM 调用"，
      而实际是 N+1 —— 多出来的那次照样花钱、照样是一次外部请求。
    """

    extras: dict[Any, dict[str, Any]] = field(
        default_factory=lambda: {
            JudgementResult: {
                "verdict": True,
                "reasoning": "(fake judge) 由 FakeLLM 自动回答，不代表真实评价",
            }
        }
    )
    """非步进调用该回什么。键是要 `output_format` 类本身。

    ★ 给 JudgementResult 一个默认答案是刻意的：不给的话库会在日志里打一条
      ERROR（它 catch 住异常后返回 None），于是每次测试输出里都有一条红色报错。
      那会让真正需要看见的报错被淹没 —— 而这正是"测试输出没人看"的开始。
    """

    extraction_text: str = "(fake) 页面抽取结果"
    """"`output_format=None` 的调用该回什么 —— 也就是【抽取类】调用。

    ★ 这一类是被"失败得很响"逼出来的：脚本里写了 extract 之后，桩抛了
      "收到未脚本化的调用类型 None"。查下去才知道 extract 动作内部会调
      `page_extraction_llm.ainvoke([SystemMessage, UserMessage])`
      （tools/service.py:1197、1276）—— 不带 output_format，要一个纯字符串。

    ★★ 这件事本身就值得记下来：**extract 不是"读页面"，是一次额外的 LLM 调用。**
      一个 run 里 extract 几次，就是几次额外的外部请求和成本。
      而它对我们是双倍的意外：我以为"只读采集"最便宜，
      实际上如果靠 extract 采数据，每次都要多付一次调用。
      （我们的采集走 output_model_schema 的结构化 done，不走 extract —— 这个选择
       因此不只是"更结构化"，也是"更省"。）

    ⚠️ 注意区分它和 other_calls 里的 judge：两者都是"额外的调用"，
      但 judge 在 run 末尾固定一次，extract 是按动作次数来的，可以有很多次。
    """

    exhausted: bool = False
    """★ 脚本用完还继续被调用 = True。

    这时桩返回一个 success=False 的 done，而不是抛异常 ——
    抛异常的话排查会指向"桩崩了"，而真相是"这次 run 的步数比脚本长"，
    后者才是需要知道的信息（护栏多拦一步、或 LLM 本该改道却没改）。
    """

    with_usage: bool = False
    """True 时返回一份假的 token 用量，用来验 token 统计链路。"""

    def bind(self, agent: Any) -> "FakeLLM":
        """接上 agent，取得 registry。返回 self 以便链式调用。"""
        self._registry = agent.tools.registry
        return self

    @property
    def model_name(self) -> str:
        """★ 库里的"legacy support"属性（llm/base.py:45-47 就是 `return self.model`）。

        它不在 BaseChatModel 的协议文档里，但 agent/cloud_events.py:217 会读它 ——
        漏了这个属性会在 run() 一开始就 AttributeError，而且报错点在"上报云事件"里，
        完全指不到"你的假 LLM 少了个属性"。鸭子类型就是这样：契约面要靠扫代码才能穷举。
        （已扫全库：对 llm 的属性访问只有 model / provider / model_name / ainvoke 四个。）
        """
        return self.model

    # ── 契约实现 ──
    async def ainvoke(
        self, messages: list[Any], output_format: Any = None, **kwargs: Any
    ) -> ChatInvokeCompletion:
        """★★ `output_format` 必须是第二个【位置】参数，不能只是关键字参数。

        踩到的坑（实测）：Agent 自己调用时是 `ainvoke(messages, **kwargs)`，
        看起来写成 `ainvoke(self, messages, **kwargs)` 就够了。
        但 TokenCostService 会猴补丁替换掉这个方法（tokens/service.py:352-397），
        而那个包装函数是这样回调的：

            result = await original_ainvoke(messages, output_format, **kwargs)

        —— 位置传参。于是签名少一个位置参数时，报错是
        "ainvoke() takes 2 positional arguments but 3 were given"，
        而且它会出现在 Agent 的重试逻辑里、被计成"失败 3/6 次"，
        报错点完全指不到"你的假 LLM 少了个位置参数"。

        这个契约不在任何文档里（已固化进 compat.py 的说明与 test_compat 的哨兵）。
        """
        step_index = len(self.calls)
        call = LLMCall(
            step_index=step_index,
            messages=list(messages),
            # 把 output_format 也记进去：它是 Agent 每步传下来的 AgentOutput 子类，
            # 断言"库给我们的 schema 是哪个"时用得上。
            kwargs={"output_format": output_format, **kwargs},
        )

        # ★ 不是步进调用（judge、抽取等）→ 走 extras，不碰脚本游标。
        if not _is_agent_output(output_format):
            self.other_calls.append(call)
            if output_format is None:
                # 抽取类调用：要一个纯字符串（见 extraction_text 的说明）。
                return self._wrap(self.extraction_text)
            scripted = self.extras.get(output_format)
            if scripted is None:
                # ★ 不猜、不魔法填充：一个没预料到的 LLM 调用类型是重要信息
                #   （库新增了一次外部请求 = 新增了成本和新的失败点）。
                #   静默糊弄过去，我们就永远不会知道它发生了。
                #   —— extract 这一类就是被这条报错逼出来的，它确实值得知道。
                raise RuntimeError(
                    f"FakeLLM 收到未脚本化的调用类型 {output_format!r}。"
                    f"库发起了一次我们没预料到的 LLM 调用。"
                    f"若这是预期的，把它加进 FakeLLM(extras={{...}})。"
                )
            return self._wrap(output_format(**scripted))

        self.calls.append(call)
        spec = self._spec_for(step_index, list(messages))
        if spec is None:
            self.exhausted = True
            return self._wrap(
                self._build_output(
                    [{"done": {"text": f"FakeLLM 脚本已耗尽（第 {step_index + 1} 次调用没有脚本）",
                               "success": False}}],
                    output_format,
                )
            )

        actions = spec if isinstance(spec, list) else [spec]
        return self._wrap(self._build_output(actions, output_format))

    # ── 内部 ──
    def _spec_for(self, step_index: int, messages: list[Any]) -> ActionSpec | list[ActionSpec] | None:
        if step_index >= len(self.script):
            return None
        item = self.script[step_index]
        # ★ 支持 callable：护栏的"被拒后改道"是【有状态】的行为
        #   （第 2 步做什么取决于第 1 步收到了什么），静态脚本表达不了。
        #   给 callable 传 messages，测试就能写出"看见 HUMAN_DENIED 就改道"的脚本。
        if callable(item):
            return item(messages, step_index)
        return item

    def _build_output(self, actions: list[ActionSpec], output_format: Any = None) -> AgentOutput:
        """把 {"click": {"index": 3}} 造成 AgentOutput。

        ★★ 这里【必须】传原始 dict，不能自己造 action 实例。踩过一次：
          `create_action_model()` 每次调用都用 create_model 造一个【新的类对象】，
          所以我自己造的 DoneActionModel 与 Agent 的 output_format 里 union 中的
          那个 DoneActionModel 是两个不同的类 —— 名字一样，isinstance 不成立。
          pydantic 报的是 "Input should be a valid dictionary or instance of
          DoneActionModel"，输入看起来明明就是一个 DoneActionModel 实例。
          排查这种"报错内容和事实矛盾"的错最费时间。

          传 dict 反而更忠实：真 LLM 返回的就是 JSON，由库自己的 union 去构造。
          顺带把"脚本里的参数是否合法"也交给库的 schema 校验，
          我们不需要也不应该复制那套规则。

        ★ output_format 优先用 Agent 传下来的那个：它是 AgentOutput 的子类
          （type_with_custom_actions 造的，action 字段被收窄成注册过的动作 union）。
          用库给的那个，等于连 schema 这一层也走了真实路径。
        """
        output_cls = output_format or AgentOutput
        for action in actions:
            for name in action:
                if name not in self._registry.registry.actions:
                    known = sorted(self._registry.registry.actions)
                    raise KeyError(f"脚本里写了不存在的动作 {name!r}；可用动作：{known}")
        return output_cls(
            evaluation_previous_goal="(fake)",
            memory="(fake)",
            next_goal=f"(fake) 第 {len(self.calls)} 步",
            action=[dict(a) for a in actions],
        )

    def _wrap(self, completion: Any) -> ChatInvokeCompletion:
        # ★ usage 必须显式传：ChatInvokeCompletion.usage 是【无默认值的必填字段】
        #   （llm/views.py:53），漏了它 pydantic 直接报错 ——
        #   搭桩时第一个必踩的坑（事实 19）。
        usage = None
        if self.with_usage:
            usage = ChatInvokeUsage(
                prompt_tokens=100, prompt_cached_tokens=0, prompt_cache_creation_tokens=None,
                prompt_image_tokens=None, completion_tokens=20, total_tokens=120,
            )
        return ChatInvokeCompletion(completion=completion, usage=usage)

    # ── 断言辅助 ──
    @property
    def steps(self) -> int:
        return len(self.calls)

    def saw_text(self, needle: str, *, step: int | None = None) -> bool:
        """LLM 是否在某一步（默认任意一步）的请求里见过某段文本。

        ★ 这是"护栏到底有没有把话说给 LLM 听"的唯一可靠验证方式。
          只看 steps.jsonl 里有 HUMAN_DENIED 是不够的 ——
          那只证明我们记了日志，不证明那段话进了提示词。

        ★ 只搜步进调用，不搜 judge：judge 的输入里会包含 agent 的整段历史，
          把它算进来会让"LLM 见过这句话"变成永远为真（自己证明自己）。
        """
        calls = self.calls if step is None else self.calls[step : step + 1]
        return any(needle in c.text() for c in calls)

    def first_step_seeing(self, needle: str) -> int | None:
        for c in self.calls:
            if needle in c.text():
                return c.step_index
        return None

    def total_image_parts(self) -> int:
        return sum(c.image_part_count() for c in self.calls)


def _is_agent_output(output_format: Any) -> bool:
    """这个 output_format 是不是步进用的 AgentOutput（含其动态子类）。

    库用的是 `AgentOutput.type_with_custom_actions(...)` 造出来的【子类】，
    所以不能比 `is`，必须用 issubclass。
    """
    return isinstance(output_format, type) and issubclass(output_format, AgentOutput)


def _text_of(message: Any) -> str:
    """从 BaseMessage 里抽出文本。★ content 可能是 str，也可能是分片列表。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text", "")))
            else:
                parts.append(str(getattr(p, "text", "")))
        return "\n".join(parts)
    return str(content)


def _image_parts_of(message: Any) -> int:
    """数一条消息里有多少个图片分片（OpenAI 的 image_url 形态）。"""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return 0
    n = 0
    for p in content:
        if isinstance(p, dict) and ("image_url" in p or p.get("type") in ("image_url", "image")):
            n += 1
        elif type(p).__name__ in ("ImageContent", "ImagePart"):
            n += 1
    return n
