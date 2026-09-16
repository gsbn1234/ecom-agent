"""browser-use 版本哨兵。

★ 存在的唯一目的：让 browser-use 升级时【主动失败】，并报出是哪几个假设破了。

  本项目对 browser-use 的依赖不只在公开 API 上，还压在四个未文档化的行为上：
    1. register_new_step_callback 的时序（LLM 输出后、action 执行前）
    2. history.get_structured_output 绕开私有字段 _output_model_schema
    3. total_duration_seconds 是方法而非 property
    4. Tools.act 的 180s action 超时
  这些散落各处的话，一次 minor 升级就是一场全库考古。
  集中成一份可执行的契约后，升级成本从「重读全库」降到「看哪条挂了」。

升级流程：改 EXPECTED_VERSION → 跑本文件 → 挂掉的每条去 docs/ADR.md 找对应条目
→ 修正实现与 ADR → 再全量回归。不要跳过本文件直接改版本号。
"""
import importlib.metadata as md
import inspect

import pytest
from browser_use import Agent, AgentHistoryList, Tools

# ★ 用 importlib.metadata 而不是 browser_use.__version__。
#   实测 0.13.10 的 browser_use 模块上【没有】__version__ 属性，写了会 AttributeError。
#   安装名是 browser-use（连字符），导入名是 browser_use（下划线），两者不是一回事 ——
#   这类"名字看着对但就是取不到"的问题，先怀疑名字本身。
EXPECTED_VERSION = "0.13.10"


def test_version_pinned():
    assert md.version("browser-use") == EXPECTED_VERSION, (
        f"browser-use 版本已变（期望 {EXPECTED_VERSION}）。"
        "本项目有四个设计建立在 0.13.10 的未文档化行为上，"
        "升级前请逐条核对本文件并读 docs/ADR.md 与 docs/spikes.md。"
    )


@pytest.mark.parametrize(
    "param",
    [
        "register_new_step_callback",   # 护栏拦截点（ADR-5）
        "register_should_stop_callback",  # 回退方案 A
        "output_model_schema",          # 结构化输出（ADR-9）
        "sensitive_data",               # 按域形态的凭据注入
        "tools",
        "browser",
    ],
)
def test_agent_constructor_still_accepts(param):
    assert param in inspect.signature(Agent.__init__).parameters


@pytest.mark.parametrize("param", ["max_steps", "on_step_start", "on_step_end"])
def test_run_still_accepts(param):
    """★ 专门盯 max_steps：它在 0.13.x 从构造函数搬到了 run()。

    这条断言存在的意义就是：如果哪天它又搬回去了，这里会红，
    而不是等到某次运行发现"设了 60 步却跑了 500 步"才发现。
    """
    assert param in inspect.signature(Agent.run).parameters


def test_action_decorator_signature():
    """★ 断言的是【真实的装饰器】Registry.action，不是 Tools.action。

    实测 Tools.action 的签名就是 (self, description, **kwargs) —— 它只把参数转发给
    self.registry.action。对着 Tools.action 断言 param_model/domains 会永远失败，
    而且失败信息读起来像"库变了"，实际是"测错了对象"。
    真正的契约在 Registry.action 上，这里盯住它。
    """
    from browser_use.tools.registry.service import Registry

    params = inspect.signature(Registry.action).parameters
    for name in ("description", "param_model", "domains", "allowed_domains", "terminates_sequence"):
        assert name in params, f"Registry.action 不再接受 {name}"


def test_special_param_names_are_reserved():
    """★★ 本文件最重要的一条：这 9 个名字是【保留字】，自定义 action 不能用。

    registry/service.py:278 的 _create_param_model 会把 SpecialActionParameters 的
    字段名从 tool schema 里整个剥掉。也就是说：如果给自定义 action 写了一个叫
    page_url 的参数，它不会被暴露给 LLM，而是被库注入当前页面 URL。

    这个失败是【静默的】：action 照常注册、照常被调用，只是 LLM 永远看不到那个参数 ——
    表现为"LLM 死活不按我说的传参"，而 schema 看起来完全正常。
    """
    from browser_use.tools.registry.views import SpecialActionParameters

    reserved = set(SpecialActionParameters.model_fields.keys())
    expected = {
        "context",
        "browser_session",
        "page_url",
        "cdp_client",
        "page_extraction_llm",
        "file_system",
        "available_file_paths",
        "has_sensitive_data",
        "extraction_schema",
    }
    assert reserved == expected, (
        f"保留参数名集合变了：多了 {reserved - expected}，少了 {expected - reserved}。"
        "新增的名字同样不能用作自定义 action 的参数名，ADR 第 4 条要同步更新。"
    )


def test_sensitive_data_is_not_a_reserved_param_name():
    """★ 红线：sensitive_data【不在】保留集合里 → 这个名字在自定义 action 里是危险的。

    不在保留集合 = 会被当普通字段原样写进 tool schema 暴露给 LLM。
    所以自定义 action 里绝不能有叫 sensitive_data 的参数（详见 docs/ADR.md 第 4 条）。

    这条断言的作用是：万一上游把它加进保留集合，行为会从"暴露给 LLM"变成"被静默注入"
    —— 两种都不是我们想要的。变化发生时立刻红，逼我们重新评估红线。
    """
    from browser_use.tools.registry.views import SpecialActionParameters

    assert "sensitive_data" not in SpecialActionParameters.model_fields


def test_agent_swallows_unknown_kwargs():
    """★ 修正一条早先记错的事实（本文件存在的主要价值之一）。

    原先记录：「Agent(context=...) 是死参数，且是静默变 None 而不是报错」。
    实测：Agent.__init__ 签名末尾是 **kwargs（service.py:213），
    而这个函数体内【从不读 kwargs】—— 从 213 行往后到下一个 kwargs 出现（1612 行，
    属于别的方法）之间零命中。

    所以 Agent(context=obj) 既不报错也不生效：对象被**直接丢弃**。

    结论不变（给自定义 action 传对象只能用闭包），但失败形态要说对：
    不是"参数被置成 None"，是"参数根本没被接收过"。
    这个区别在排查时很关键 —— 前者会让你去 Agent 内部找赋值点，而那里根本没有赋值点。
    """
    sig_params = inspect.signature(Agent.__init__).parameters
    assert "context" not in sig_params, "Agent 开始接受 context 了，闭包方案可以简化"
    assert "kwargs" in sig_params

    src = inspect.getsource(Agent.__init__)
    assert "kwargs[" not in src and "kwargs.get" not in src, (
        "Agent.__init__ 开始读 kwargs 了 —— context 相关的结论需要重新验证"
    )


def test_history_accessors_we_depend_on():
    # 事实 11：私有字段 _output_model_schema 序列化后丢失 → 必须用这个 getter
    assert hasattr(AgentHistoryList, "get_structured_output")
    for name in (
        "final_result",
        "action_results",
        "errors",
        "model_thoughts",
        "number_of_steps",
        "screenshots",
        "screenshot_paths",
        "urls",
    ):
        assert hasattr(AgentHistoryList, name), f"AgentHistoryList 缺少 {name}"


def test_total_duration_seconds_is_a_method_not_a_property():
    """★ 这条抓一个静默出错的坑。

    实测 agent/views.py:603 是 `def total_duration_seconds(self)`，【没有】@property。
    写成 history.total_duration_seconds 会拿到 bound method —— 不报错，
    直到序列化进 JSON 时才炸，而那时栈已经离现场很远了。
    （早期一份探查报告把它记成 property，是错的。只有 structured_output 是真 property。）

    用 getattr_static 而不是 getattr：后者会触发描述符协议，
    property 和普通方法都会被解析成"可调用对象"，两种情形下断言都会通过 —— 测了个寂寞。
    """
    raw = inspect.getattr_static(AgentHistoryList, "total_duration_seconds")
    assert not isinstance(raw, property), (
        "它变成 property 了：所有调用点要把 () 去掉，compat.get_run_stats 也要改"
    )
    assert callable(getattr(AgentHistoryList, "total_duration_seconds"))


def test_interrupted_error_is_reraised_by_agent():
    """回退方案 A（停机重跑）的可靠性前提。

    agent/service.py:2830-2833 对 InterruptedError 是显式 `raise` 而非吞掉，
    所以停机回调抛的 InterruptedError 能干净穿透。
    注意这与「自定义 action 函数体内抛的 InterruptedError 会被 Tools.act 吞掉」
    不矛盾 —— 是不同栈帧。这里只能断言源码里那个 re-raise 还在，
    真实行为由 Phase 2 的 spike 验证。
    """
    import browser_use.agent.service as svc

    src = inspect.getsource(svc)
    assert "isinstance(e, InterruptedError)" in src, (
        "停机信号的穿透保证不见了，回退方案 A 需要重新验证（见 docs/ADR.md 第 5 条）"
    )
