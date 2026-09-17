"""runner / recorder / 落库 的离线测试 —— 零浏览器、零网络、零 token。

★ 为什么这个文件非有不可，而不是"等 e2e 一起测"：
  Phase 3 的两条硬约束（增长闸门、LLM 总调用计数）一旦写错，
  失败形态都是**静默**的 —— 盘上多一行少一行、账上多一次少一次，
  而报告看起来完全正常。用真浏览器 + 真 LLM 去验它们的话，
  每次失败都要先排除"模型今天不听话"，而真正的信号只有一行 JSONL 的差别。

  这里用**合成的 AgentHistory** 直接把机制逼到墙角：不启动浏览器，
  但走的是 recorder → 脱敏 → 写盘 的真实代码路径。

★ 合成 AgentHistory 而不是自己写一个假对象，是刻意的：
  假对象只能证明"我的代码和我的假设一致"。用库真的 AgentHistory /
  StepMetadata / BrowserStateHistory，才能顺带证明
  `compat.extract_step_facts` 对这些形状的读法是对的
  —— 而那一层是"库改版时唯一会静默失效的地方"。
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from browser_use.agent.views import AgentHistory, AgentOutput, BrowserStateHistory, StepMetadata
from ecom_agent.config import TASKS_DIR
from ecom_agent.dsl.compiler import compile_task
from ecom_agent.dsl.loader import load_task
from ecom_agent.guardrails.interceptor import GuardrailInterceptor
from ecom_agent.guardrails.policy import GuardrailPolicy
from ecom_agent.observability.models import LlmUsage, RunRecord
from ecom_agent.observability.recorder import RunRecorder
from ecom_agent.runtime import runner as R
from ecom_agent.runtime.cli import _exit_code, _parse_params
from ecom_agent.sites.pinduoduo.output_models import ParseStatus

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
"""★ 头 8 字节是真的 PNG magic，后面是垃圾。

  这对本项目的测试是**正确**的素材：recorder 的判据就是 magic bytes
  （它能拿到的最强判据，见 recorder.PNG_MAGIC 的说明）。
  如果哪天判据变成"能不能解码出完整 PNG"，这个常量会红 ——
  那正是需要的提醒：判据变强了，测试素材也要跟着变强。"""


# ── 素材 ──────────────────────────────────────────────────
@pytest.fixture(scope="module")
def books_spec():
    return load_task(TASKS_DIR / "books_demo.yaml")


@pytest.fixture(scope="module")
def pdd_spec():
    return load_task(TASKS_DIR / "pdd_search_products.yaml")


@pytest.fixture(scope="module")
def pdd(pdd_spec):
    """★ `keyword` 是**必填**参数（pdd_search_products.yaml 里没有 default），
    所以编译 pdd 模板时必须给 —— 编译期缺参数会抛 ParamError，这是设计使然。"""
    return compile_task(pdd_spec, {"keyword": "保温杯"})


@pytest.fixture
def books(books_spec):
    return compile_task(books_spec)


_TOOLS_CACHE: list[Any] = []
_OUTPUT_CLS_CACHE: list[Any] = []


def _tools():
    """本项目注册过的 Tools，**进程内只造一次**。

    ★ 必须缓存：`registry.create_action_model()` 每次调用都用
      pydantic `create_model` 造一个**新的类对象**（名字一样，`is` 不成立）。
      每次新建的话，"脚本/历史项里那个 scroll 动作"和"output_cls 的 union 里
      那个 ScrollActionModel"是两条不同的类，校验会报一条完全指不到原因的错。
    """
    if not _TOOLS_CACHE:
        from ecom_agent.actions.guard_gate import build_tools

        _TOOLS_CACHE.append(build_tools())
    return _TOOLS_CACHE[0]


def _output_cls():
    """`Agent` 构造时用的那个 AgentOutput **子类**（动作字段被收窄成注册表里的 union）。

    ★★ 这里踩了一个坑，值得记下来：不能直接用 `browser_use.agent.views.AgentOutput`。

      它的 `action` 字段类型是 `list[tools.registry.views.ActionModel]`，而那个
      `ActionModel` 是个**空模型 + extra=forbid**（`model_fields` 是 `{}`）。
      往它塞 `{"scroll": {...}}` 报的是：

          action.0.scroll
            Extra inputs are not permitted [input_value={'down': True, 'pages': 1.0}]

      —— 报错位置指向 `scroll` 的**参数**，读起来像"scroll 的参数写错了"，
      而真相是"这个模型里根本没有 scroll 这个动作"。照着报错去改参数只会越修越远。

      真正能用的是 Agent 自己造的那个子类（`type_with_custom_actions`，
      service.py:786-790）。★ 顺带说明了一件事：**AgentOutput 这个基类
      在 0.13.10 上单独用是跑不了任何动作的**，它必须被 registry 收窄过。
    """
    if not _OUTPUT_CLS_CACHE:
        _OUTPUT_CLS_CACHE.append(
            AgentOutput.type_with_custom_actions(_tools().registry.create_action_model())
        )
    return _OUTPUT_CLS_CACHE[0]


def make_history_item(
    step: int,
    *,
    url: str = "https://books.toscrape.com/",
    actions: list[dict[str, Any]] | None = None,
    start: float = 1000.0,
    end: float = 1002.5,
) -> AgentHistory:
    """造一条真的 AgentHistory。

    ★ action 传原始 dict 而不是自己造动作实例：库的动作模型类是
      `create_action_model()` **每次调用新造**的（FakeLLM 的注释里记了这次踩坑），
      自己造实例会因为"名字一样但是两个类"而报一条看不懂的校验错。
      传 dict 让库自己的 union 去构造 —— 顺便把"参数符合库的 schema"也验了。
    """
    return AgentHistory(
        model_output=_output_cls()(
            evaluation_previous_goal="(test)",
            memory="(test)",
            next_goal=f"(test) 第 {step} 步",
            action=list(actions or []),
        ),
        result=[],
        state=BrowserStateHistory(
            url=url, title=f"第 {step} 页", tabs=[], interacted_element=[]
        ),
        metadata=StepMetadata(step_start_time=start, step_end_time=end, step_number=step),
    )


class FakeAgent:
    """`on_step_end` 钩子需要的最小 agent 形状：`.history.history` 与 `.state.n_steps`。

    ★ 只有这两个是钩子真正读的东西。做成最小形状是刻意的：
      钩子多读一个属性，这里就得多补一个 —— 于是"钩子对 agent 的依赖面"
      在测试里变成可数的，而不是靠读源码估。
    """

    def __init__(self) -> None:
        self.history = type("H", (), {"history": []})()
        self.state = type("S", (), {"n_steps": 1})()


def make_recorder(tmp_path: Path, **kw: Any) -> RunRecorder:
    return RunRecorder(
        run_id="test-run", task_id="demo.test", run_dir=tmp_path / "run", **kw
    )


def make_interceptor(compiled, recorder) -> GuardrailInterceptor:
    return GuardrailInterceptor(
        GuardrailPolicy(compiled.spec.guardrails),
        None,  # 审批通道：本文件里不会走到需要它的分支
        recorder=recorder,
        run_id="test-run",
    )


def make_runner(compiled, **kw: Any) -> R.TaskRunner:
    return R.TaskRunner(compiled, **kw)


# ── A. 预检：纯函数，在创建浏览器之前 ─────────────────────
def test_check_start_url_passes_for_whitelisted(books):
    assert R.check_start_url(books) is None


def test_check_start_url_rejects_off_whitelist(pdd):
    """★ 把 start_url 改到白名单外，预检必须拦住并说清"这是配置错误"。

    ★★ 这条测试守的是一个曾经**根本不存在**的检查点：
      `start_url` 在渲染 task_text 时不被使用（task_text 只有 goal/步骤/参数/
      分页/护栏条款），也没有任何人拿它去导航 —— 一个声明了却无人执行的字段。
      把它改坏不会报任何错，agent 从 about:blank 起步然后自己猜一个 URL。
    """
    spec = pdd.spec.model_copy(update={"start_url": "https://evil.example.com/x"})
    problem = R.check_start_url(compile_task(spec, pdd.params))
    assert problem is not None
    assert "白名单" in problem
    assert "evil.example.com" in problem


def test_check_start_url_requires_a_value(books_spec):
    compiled = compile_task(books_spec.model_copy(update={"start_url": ""}))
    assert "start_url" in (R.check_start_url(compiled) or "")


def test_preflight_runs_before_browser_and_before_artifacts(books, tmp_path, monkeypatch):
    """★★ 配置错误必须发生在**任何产物产生之前**。

    ★ 为什么这条值得单独测：如果 `_resolve_llm` 晚于 `RunRecorder(...)`，
      那么"忘了配 key"的表现就是 **`runs/` 下多一个空目录**
      —— 而那个空目录看起来像"跑过一次"，排查的人会去里面找遗漏的产物，
      而不是去看那个真正的原因（没配 key）。
    """
    monkeypatch.setattr(R, "LIVE_LLM", False)
    runs_dir = tmp_path / "runs"
    with pytest.raises(R.PreflightError) as ei:
        import asyncio

        asyncio.run(make_runner(books, runs_dir=runs_dir).run())
    assert "ECOM_AGENT_ENABLE_LIVE_LLM" in str(ei.value)
    assert not runs_dir.exists(), "预检失败却留下了产物目录"


def test_unrunnable_rule_actions_flags_unregistered_actions(books, pdd):
    """★ 规则里引用了未注册的动作 → 那条规则**永远不会命中**，必须在启动时说出来。

    ⚠️ 这条断言绑在一个**已知的真实状态**上：两个 YAML 的 allow-readonly 规则
      都写了 `extract_table`，而它不是 browser-use 的内置动作（内置 24 个里没有），
      我们自己的实现要等 Phase 4。所以现在它必须被标出来。

      Phase 4 注册了 extract_table 之后这条会红 —— 那时**改测试**就是对的，
      因为"这条规则是摆设"这个事实消失了。★ 这正是想要的：它逼着人去面对
      "规则集和注册表对不对得上"，而不是让这个错位一直躺着。
    """
    tools = _tools()
    assert ("allow-readonly", "extract_table") in R.unrunnable_rule_actions(books, tools)
    assert ("allow-readonly", "extract_table") in R.unrunnable_rule_actions(pdd, tools)


def test_unrunnable_rule_actions_passes_for_the_real_registry(books):
    """★ 反面的对照：**内置动作**的名字不该被误报。

      没有这条的话，上面那条测试在一个"永远返回全部规则"的实现下也会通过 ——
      而那种实现等于每次启动都刷一屏假警告，很快就会没人看它。
    """
    flagged = dict(
        (action, rule) for rule, action in R.unrunnable_rule_actions(books, _tools())
    )
    for name in ("click", "input", "scroll", "go_back", "extract", "navigate", "done"):
        assert name not in flagged, f"{name} 是内置动作，不该被判成不可运行"


def test_unrunnable_rule_actions_silent_when_registry_unreadable(books):
    """★ 拿不到注册表就**不表态**，而不是"全都不可用"。

      假警报比不报警更坏：它训练人忽略这个检查，而真正该报警的那次
      就跟着一起被忽略了。（同 compat.action_model_fields 的教训。）
    """
    assert R.unrunnable_rule_actions(books, tools=object()) == []


# ── B. 状态判定与重试 ─────────────────────────────────────
@pytest.mark.parametrize(
    ("structured", "rows", "blocked", "error", "expected"),
    [
        (object(), 5, False, "", ParseStatus.OK.value),
        (object(), 0, False, "", ParseStatus.EMPTY.value),
        (object(), 5, True, "", ParseStatus.BLOCKED.value),
        (None, 0, False, "", ParseStatus.SCHEMA_INVALID.value),
        (object(), 5, False, "boom", ParseStatus.SCHEMA_INVALID.value),
        # ★ blocked 优先于一切：被护栏拦停时即使拿到了数据也不算 ok，
        #   因为那批数据的完整性没人保证过（run 是在中途被停的）。
        (object(), 5, True, "boom", ParseStatus.BLOCKED.value),
    ],
)
def test_classify_parse_status(structured, rows, blocked, error, expected):
    assert R.classify_parse_status(
        structured=structured, rows=rows, blocked=blocked, error=error
    ) == expected


def test_should_retry_never_retries_blocked(books_spec):
    """★ 被护栏拦停 **不重试**（计划里的明确决定）。

      重试只会再撞一次同一堵墙 —— 唯一稳定的产出是"多烧一倍的 token"，
      而且第二次的 attempt 数会让报告看起来像"偶发失败"，
      掩盖掉"规则集需要人来改"这个真实结论。
    """
    compiled = compile_task(books_spec)
    assert R.should_retry(compiled, ParseStatus.BLOCKED.value) is False


def test_should_retry_empty_follows_yaml(books, pdd):
    """★ `on_empty_result` 必须真的起作用，否则它就是个装饰。

      books_demo = accept（练手站点上"没搜到"是合法结局）
      pdd        = retry（商家后台零行更可能是没加载完，值得再来一次）
    """
    assert R.should_retry(books, ParseStatus.EMPTY.value) is False
    assert R.should_retry(pdd, ParseStatus.EMPTY.value) is True


def test_should_retry_ok_never_and_schema_invalid_always(books_spec):
    compiled = compile_task(books_spec)
    assert R.should_retry(compiled, ParseStatus.OK.value) is False
    assert R.should_retry(compiled, ParseStatus.SCHEMA_INVALID.value) is True


# ── C. LLM 账：先观测、后解释 ─────────────────────────────
def _step_with_actions(*names: str):
    return type("S", (), {"actions": [type("A", (), {"name": n})() for n in names]})()


class _Ledger:
    """库自己的令牌账本（`agent.token_cost_service.usage_history`）的形状。"""

    def __init__(self, n: int, prompt: int = 0, completion: int = 0) -> None:
        self.usage_history = [
            type("E", (), {"usage": type("U", (), {
                "prompt_tokens": prompt, "completion_tokens": completion})()})()
            for _ in range(n)
        ]


class _CountingAgent:
    """`interpret_llm_usage` 需要的最小 agent 形状。

    ★ 只有两处依赖（都在 compat 里）：`AgentOutput` 的**名字**、以及令牌账本。
      名字用假类来喂，是为了能构造"库改了命名"这个场景 —— 那是分类会
      **静默全错**的唯一成因。
    """

    def __init__(self, step_name: str = "AgentOutput", ledger: _Ledger | None = None) -> None:
        if step_name:
            self.AgentOutput = type(step_name, (), {})
        self.token_cost_service = ledger


def test_interpret_llm_usage_splits_by_output_format_name():
    """★ 分类靠**问 agent 要类型名**，不靠写死常量。

      写死 `"AgentOutput"` 的话，库换个命名就会让 step/judge 一起变成 0 ——
      而 0 看起来像"这次没调用裁判"，不像 bug。
    """
    usage = LlmUsage(total_calls=5, by_format={"MySteps": 3, "JudgementResult": 1, "none": 1})
    agent = _CountingAgent("MySteps", _Ledger(4))
    steps = [_step_with_actions("click"), _step_with_actions("extract"), _step_with_actions("done")]

    R.interpret_llm_usage(usage, agent=agent, steps=steps)

    assert usage.step_calls == 3
    assert usage.judge_calls == 1
    assert usage.extract_calls == 1
    # 残差 = 5 - 3 - 1 - 1 = 0
    assert usage.other_calls == 0
    assert usage.unexplained() == 0
    assert usage.library_calls == 4


def test_interpret_llm_usage_residual_lands_in_other_not_in_unexplained():
    """★★ 这里钉住一个**反直觉**的事实，钉住它比"知道它"重要：

      因为 other 定义成残差，正残差会被它吸收干净 —— 账面上
      `unexplained()` 恒为 0。所以"多出一类我们不认识的调用"**不能靠
      unexplained() 发现**，要看 `other_calls` 本身非不非零。

      为什么值得为一条"数字怎么摆"写测试：如果哪天有人把 other 改成
      `total - step - judge`（看着更自然），extract 就会被重复计入，
      other 会变成负数、被 clamp 成 0，于是那类**真正需要人去查的调用**
      就彻底隐形了。这条断言是那种改动的唯一拦路。
    """
    usage = LlmUsage(total_calls=6, by_format={"AgentOutput": 3, "JudgementResult": 1})
    R.interpret_llm_usage(usage, agent=_CountingAgent("AgentOutput"), steps=[])

    assert usage.other_calls == 2, "没归类的调用必须留在 other_calls 里可见"
    assert usage.unexplained() == 0, "正残差被 other 吸收 → 账是平的（这是刻意的）"
    assert "无法归类" in usage.summary()
    assert "2" in usage.summary()


def test_interpret_llm_usage_negative_residual_says_counted_too_many():
    """★ 反方向的差额（解释出的比观测到的多）必须**用不同的话**说出来。

      真实成因：`extract` 动作记进了 steps.jsonl，但它内部的 LLM 调用
      在发出去之前就失败了 —— 于是我们数了 1 次，观测是 0 次。

      写成"有 -1 次未被解释"会读成一个可以忽略的小毛病，
      而它的实际含义是"我们对这个库的调用模型在某条路径上是错的"。
    """
    usage = LlmUsage(total_calls=1, by_format={"AgentOutput": 1})
    steps = [_step_with_actions("extract"), _step_with_actions("extract")]
    R.interpret_llm_usage(usage, agent=_CountingAgent("AgentOutput"), steps=steps)

    assert usage.step_calls == 1
    assert usage.extract_calls == 2
    assert usage.other_calls == 0, "负残差不能变成负的 other_calls"
    # 1（观测）− 1（步进）− 2（抽取）= −2
    assert usage.unexplained() == -2
    assert "数多了，不是数漏了" in usage.summary()


def test_interpret_llm_usage_token_fallback_only_when_we_counted_nothing():
    """★ 库的账本只在"我们自己一个 token 都没数到"时兜底。

      两个来源都累加会**翻倍**，而翻倍看起来像"这个任务确实很贵" ——
      一个不会报错、只会让成本判断一直偏高的错误。
    """
    usage = LlmUsage(total_calls=1, by_format={"AgentOutput": 1}, prompt_tokens=100,
                     completion_tokens=20)
    R.interpret_llm_usage(usage, agent=_CountingAgent("AgentOutput", _Ledger(3, 999, 999)),
                          steps=[])
    assert (usage.prompt_tokens, usage.completion_tokens) == (100, 20)

    empty = LlmUsage(total_calls=1, by_format={"AgentOutput": 1})
    R.interpret_llm_usage(empty, agent=_CountingAgent("AgentOutput", _Ledger(3, 999, 999)),
                          steps=[])
    assert (empty.prompt_tokens, empty.completion_tokens) == (999 * 3, 999 * 3)


def test_library_calls_is_none_when_ledger_absent():
    """★ 没有账本时是 **None**，不是 0。

      0 会被读成"库说是零次调用" —— 一个完全不同的结论（而且是个吓人的结论）。
      这个字段是独立对照，它的价值全在"两边各自数"上。
    """
    usage = LlmUsage(total_calls=1, by_format={"AgentOutput": 1})
    R.interpret_llm_usage(usage, agent=_CountingAgent("AgentOutput", None), steps=[])
    assert usage.library_calls is None


# ── D. 结构化输出解析 ─────────────────────────────────────
class _FakeHistory:
    def __init__(self, final: Any, structured: Any = None, raises: Exception | None = None):
        self._final = final
        self._structured = structured
        self._raises = raises

    def final_result(self):
        return self._final

    def get_structured_output(self, model):
        if self._raises is not None:
            raise self._raises
        return self._structured


def test_extract_structured_returns_none_when_llm_never_called_done():
    got, err = R._extract_structured(_FakeHistory(final=None), dict)
    assert got is None
    assert "done" in err


def test_extract_structured_catches_validation_error_as_quarantine():
    """★★ 校验失败必须被**接住**，不能让它冒出去。

      它是四档容错里的第 4 档（quarantine）—— **预期内的结局**，不是崩溃。
      让它冒出去的话，run 会在 `_extract_structured` 处抛出，
      于是 result.json 和 report.html 都不会被写出来 ——
      而那份"不符合 schema 的原文"恰恰是这时唯一该看的东西。
    """
    from pydantic import ValidationError

    try:
        RunRecord(run_id="x", task_id="y", 不存在的字段=1)  # noqa: N803
    except ValidationError as exc:
        boom = exc
    else:  # pragma: no cover - extra=forbid 保证这里到不了
        raise AssertionError("extra=forbid 没生效，下面的断言失去意义")

    got, err = R._extract_structured(
        _FakeHistory(final="{...}", raises=boom), dict
    )
    assert got is None
    assert "quarantine" in err
    # ★ 只留前几行：pydantic 的完整报错可能上百行，
    #   塞进 run.json 会把真正该读的字段挤到看不见的地方。
    assert err.count("\n") <= 12


def test_extract_structured_passes_through_other_exceptions():
    got, err = R._extract_structured(_FakeHistory(final="{...}", raises=RuntimeError("炸了")), dict)
    assert got is None
    assert "RuntimeError" in err and "炸了" in err


def test_row_count_tolerates_missing_rows():
    assert R._row_count(None) == 0
    assert R._row_count(object()) == 0
    assert R._row_count(type("M", (), {"rows": [1, 2, 3]})()) == 3


# ── E. 增长闸门（Phase 3 硬约束之一）──────────────────────
def _drive(hook, agent, recorder) -> int:
    """跑一次钩子，返回 steps.jsonl 当前的行数。

    ★ 断言一律落在**行数**上：这个文件里要挡的失败形态是"多一行 / 少一行"，
      而多出来的那一行内容完全合法（有 URL、有截图、有动作、有结果），
      唯一会露出来的地方就是行数。
    """
    import asyncio

    asyncio.run(hook(agent))
    path = recorder.steps_path
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def test_step_hook_records_only_when_history_grows(tmp_path, books, monkeypatch):
    """★★★ 整个 Phase 3 最关键的一条断言。

      库在 `_execute_step` 里**无条件**调 `on_step_end`（service.py:2481-2482），
      也就是无论成功、失败、被中断都会调。而"这一步有没有产生历史项"
      是**另一件事**：`step()` 的 finally 调 `_finalize`，而 `_finalize`
      第一行 `if not self.state.last_result: return`（service.py:1357-1358）
      —— 直接返回，不追加历史项，也不推进 n_steps。

      于是那些情况下 `history.history[-1]` 指的是**上一步**。
      直接记，就是把上一步记两遍：盘上多一行，而那一行内容完全合法
      （有 URL、有截图、有动作、有结果），**没有任何一处会报错**。

      ★ 这条测试要挡的就是"两遍"—— 它断言的是**行数**，
        因为行数是这个失败形态唯一会露出来的地方。
    """
    recorder = make_recorder(tmp_path)
    interceptor = make_interceptor(books, recorder)
    runner = make_runner(books)
    hook = runner._make_step_hook(recorder, interceptor)

    agent = FakeAgent()
    agent.history.history.append(make_history_item(1))
    agent.state.n_steps = 1

    assert _drive(hook, agent, recorder, ) == 1

    # ── 关键：历史**没有**变长（模拟被中止 / 空动作），再调一次 ──
    assert _drive(hook, agent, recorder, ) == 1, (
        "历史没变长却又写了一行 —— 上一步被记了两遍"
    )

    # ── 真的变长了 → 记第二行 ──
    agent.history.history.append(make_history_item(2))
    agent.state.n_steps = 2
    assert _drive(hook, agent, recorder, ) == 2

    steps = [json.loads(l) for l in recorder.steps_path.read_text(encoding="utf-8").splitlines()]
    assert [s["step"] for s in steps] == [1, 2], "记的必须是严格意义上的最后一项"


def test_step_hook_uses_library_step_number_not_our_own_counter(tmp_path, books):
    """★ 步号取历史项自己的 `metadata.step_number`。

      那是库记账用的数字，和 `new_step_callback` 拿到的 step_index 同源
      （都是 `state.n_steps`），所以护栏的记录和步记录能对上。
      自己维护计数器的话，一旦库在某条路径上跳号或补号，
      我们和它的编号会**悄悄分叉** —— 而报告里两边是并排显示的。
    """
    recorder = make_recorder(tmp_path)
    interceptor = make_interceptor(books, recorder)
    runner = make_runner(books)
    hook = runner._make_step_hook(recorder, interceptor)

    agent = FakeAgent()
    # 库在第 7 步（比如因为前几步都是空动作没记历史），我们自己的计数是 1
    agent.history.history.append(make_history_item(7))
    agent.state.n_steps = 7
    _drive(hook, agent, recorder, )

    line = json.loads(recorder.steps_path.read_text(encoding="utf-8").splitlines()[0])
    assert line["step"] == 7


def test_step_hook_drops_decisions_collected_on_an_aborted_step(tmp_path, books):
    """★ 被中止的那一步：判定确实发生过，但**没有历史项可挂**。

      这里断言的是"不崩、且不写行"。判定本身会被丢掉 —— 这一点是**诚实的
      缺陷**，写在 docstring 里而不是假装没发生：丢掉的那条恰好落在硬停路径上，
      也就是最需要证据的地方。真要留住它，得让记录器和库的中止路径解耦，
      那是 Phase 4 之后的事（现在留一条 debug 日志 + 这个显式断言）。
    """
    recorder = make_recorder(tmp_path)
    interceptor = make_interceptor(books, recorder)
    runner = make_runner(books)
    hook = runner._make_step_hook(recorder, interceptor)

    agent = FakeAgent()
    agent.history.history.append(make_history_item(1))
    _drive(hook, agent, recorder, )

    from ecom_agent.observability.models import GuardrailDecisionRecord

    interceptor._pending_decisions.append(
        GuardrailDecisionRecord(rule_id="block-destructive", decision="block", reason="(test)")
    )
    assert _drive(hook, agent, recorder, ) == 1, "没历史项就不该写行"
    assert interceptor.take_decisions() == [], "攒下的判定要被取走，不能在下一步串味"


def test_actions_not_executed_flag_stays_false_on_the_normal_path(tmp_path, books):
    """★ `actions_not_executed` 在当前库版本上是**防御性**的，这条测试把这个事实钉住。

      按增长闸门的推理，"被中止的步"根本走不到 `_record_step`
      （`_finalize` 早退 → 历史不增长 → 闸门挡掉）——
      所以正常情况下它恒为 False。留着这个字段的理由是
      `_finalize` 的早退条件是库的内部实现，它一变闸门就挡不住了，
      而那时这一行是唯一还能指出"结果不可信"的东西。

      ★ 测试写"现在是 False"而不是"永远不该为 True"：
        后者会让 Phase 4 里一个正当的修复变成"测试红了"。
    """
    recorder = make_recorder(tmp_path)
    interceptor = make_interceptor(books, recorder)
    runner = make_runner(books)
    hook = runner._make_step_hook(recorder, interceptor)

    agent = FakeAgent()
    agent.history.history.append(make_history_item(1))
    _drive(hook, agent, recorder, )

    line = json.loads(recorder.steps_path.read_text(encoding="utf-8").splitlines()[0])
    assert line["actions_not_executed"] is False
    assert interceptor.aborted_step is None


def test_recorded_step_carries_real_values_from_the_history_item(tmp_path, books):
    """★ 确认真值真的从库里读出来了（不是一路默认值走到底）。

      一条"字段都在、但全是空的"记录是这套系统最危险的产出形态：
      报告能渲染、schema 校验能过、测试能绿，而回放时什么也回答不了。
      所以这里不只断言"有这一行"，而是断言**具体的值**对得上。
    """
    recorder = make_recorder(tmp_path)
    interceptor = make_interceptor(books, recorder)
    runner = make_runner(books)
    hook = runner._make_step_hook(recorder, interceptor)

    agent = FakeAgent()
    agent.history.history.append(
        make_history_item(1, url="https://books.toscrape.com/catalogue/page-2.html",
                          actions=[{"scroll": {"down": True, "pages": 1.0}}])
    )
    _drive(hook, agent, recorder, )

    line = json.loads(recorder.steps_path.read_text(encoding="utf-8").splitlines()[0])
    assert line["url"] == "https://books.toscrape.com/catalogue/page-2.html"
    assert line["title"] == "第 1 页"
    assert [a["name"] for a in line["actions"]] == ["scroll"]
    assert line["duration_s"] == 2.5, "耗时来自 metadata 的两个时间戳之差"
    assert line["attempt"] == 1
    assert line["model_thought"], "mode_thought 为空说明 extract_step_facts 没读到动作输出"


# ── F. 记录器：S2-4 探测器 + 重试分目录 ───────────────────
def test_empty_selector_map_is_detected_and_survives_into_run_record(tmp_path):
    """★★ S2-4 那个静默失效的探测器，端到端验一遍。

      ★ 后半段断言（`RunRecord(**stats)` 能构造）守的是一个**具体的**
        已发生过的 bug：`empty_selector_map_steps` 一度只在 recorder 里被计数、
        却没有任何 `RunRecord` 字段接住它 —— 于是 S2-4 的探测器**算出来了，
        而没有任何人会看到**（`observation_stats()` 的返回值是
        `RunRecord(**stats)` 形式的合并，多出来的键在 extra=forbid 下报错，
        漏掉的键静默消失）。

        计数器算出来却没人看得到，和没算是同一件事 —— 只是更难发现，
        因为代码看起来是在防这件事的。
    """
    recorder = make_recorder(tmp_path)
    recorder.stash_snapshot(
        {"url": "https://x/", "title": "t", "element_texts": {}, "selector_map_size": 0},
        None,
        step_index=3,
    )
    assert recorder.empty_selector_map_steps == [3]

    stats = recorder.observation_stats()
    record = RunRecord(run_id="r", task_id="t", **stats)  # ← 多/少键都会在这里炸
    assert record.empty_selector_map_steps == [3]


def test_snapshot_overwrite_records_the_victim_step(tmp_path):
    """★ 覆盖计数要记**被覆盖的那一步**，不是覆盖者。

      记错对象的话，报告会指着一个完好的步说"它有错帧"，
      而真正错位的那一行看起来完全正常（有 URL、有图、有动作）。
    """
    recorder = make_recorder(tmp_path)
    fields = {"url": "https://x/", "title": "t", "element_texts": {}, "selector_map_size": 4}
    recorder.stash_snapshot(fields, None, step_index=1)
    recorder.stash_snapshot(fields, None, step_index=2)

    assert recorder.snapshot_overwrites == 1
    assert recorder.overwritten_steps == [1]


def test_begin_attempt_clears_cross_attempt_leftovers(tmp_path):
    """★ 切 attempt 必须清掉交接槽与 `_last_sha`。

      不清槽 → 记录里会出现"属于 attempt 2、却是 attempt 1 的页面"的行。
      不清 `_last_sha` → 重试的第一步会被标成"画面与上一步相同"，
      而它比较的是两个**不同 attempt** 的图 —— 一个真实存在的误报，
      而且它指向的东西根本不存在（两个 attempt 之间没有"上一步"）。
    """
    recorder = make_recorder(tmp_path)
    fields = {"url": "https://a/", "title": "t", "element_texts": {}, "selector_map_size": 4}
    recorder.stash_snapshot(fields, None, step_index=1)
    recorder._last_sha = "deadbeef"

    recorder.begin_attempt(2)

    assert recorder._pending is None and recorder._last_sha is None
    assert recorder.attempt == 2

    import asyncio

    rec = asyncio.run(
        recorder.record_step(make_history_item(1), step_index=1, duration_s=0.0)
    )
    assert rec.snapshot_missing is True, "attempt 2 不该复用 attempt 1 的快照"


def test_screenshots_are_partitioned_by_attempt(tmp_path):
    """★★ 截图按 attempt 分目录 —— 不分就是**静默覆盖**。

      重试的第 2 次从 step=1 重新编号，于是 attempt-1 的图被 attempt-2 的图
      覆盖掉，而两次记录里的路径都写着 `screenshots/step-001.png`。
      报告看起来完全正常（有图、有路径、能打开），只是 attempt-1 的那张
      已经不在了 —— 而那正是排查"第一次为什么失败"唯一想看的东西。
    """
    recorder = make_recorder(tmp_path)
    b64 = base64.b64encode(PNG_BYTES).decode()

    rel1, sha1 = _persist(recorder, 1, b64)
    recorder.begin_attempt(2)
    rel2, sha2 = _persist(recorder, 1, b64)

    assert rel1 != rel2, "两个 attempt 的同号步不能落到同一个文件"
    assert rel1.endswith("step-001.png") and "a01" in rel1
    assert "a02" in rel2
    assert (recorder.run_dir / rel1).exists() and (recorder.run_dir / rel2).exists()
    assert sha1 == sha2, "同一个字节流的 sha 必须相同（否则重命名逻辑动了内容）"


def test_non_png_screenshot_is_absent_rather_than_broken(tmp_path):
    """★ 不是真 PNG 就**不写文件**。

      写下去会得到一个看起来正常、打开是坏图的产物，而且它混在一堆好图里。
      宁可这一步"没有截图"并在 run.json 里多一个计数 ——
      **缺席是诚实的，坏图是骗人的。**
    """
    recorder = make_recorder(tmp_path)
    bad = base64.b64encode(b"<html>not a png</html>").decode()

    rel, sha = _persist(recorder, 1, bad)

    assert rel is None and sha is None
    assert recorder.broken_screenshot_steps == [1]
    assert not list(recorder.screenshots_dir.rglob("*.png"))


def test_screenshot_can_be_turned_off_by_the_task(tmp_path):
    recorder = make_recorder(tmp_path, screenshot=False)
    rel, sha = _persist(recorder, 1, base64.b64encode(PNG_BYTES).decode())
    assert (rel, sha) == (None, None)
    assert recorder.broken_screenshot_steps == [], "关掉截图不算坏图"


def _persist(recorder, step, b64):
    import asyncio

    return asyncio.run(recorder._persist_screenshot(step, b64))


# ── G. 落库：新列真的进了 schema 和 INSERT ────────────────
def test_empty_selector_map_steps_reach_sqlite(tmp_path):
    """★★ 端到端验 store 那条链：schema 列 → ALTER 迁移 → INSERT → 读回。

      为什么要端到端而不是分开测三处：这三处**必须同时改**，而只改一处的
      后果是"新库和老库分叉"—— 分叉表现为"在我机器上是好的"（我的库是新键的），
      也就是最难在 CI 里暴露的那种失败。
    """
    import sqlite3

    from ecom_agent.store.repository import Repository

    db = tmp_path / "t.db"
    record = RunRecord(
        run_id="2026-01-01T00:00:00+00:00-abc123",
        task_id="demo.books",
        status="completed",
        parse_status=ParseStatus.OK.value,
        empty_selector_map_steps=[2, 3],
        snapshot_overwrites=1,
        redaction_counts={"phone": 2},
    )
    with Repository(db) as repo:
        repo.save_run(record, (), redactor=None)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (record.run_id,)).fetchone()
    conn.close()

    assert json.loads(row["empty_selector_map_steps_json"]) == [2, 3]
    assert row["snapshot_overwrites"] == 1
    assert json.loads(row["redaction_counts_json"]) == {"phone": 2}


def test_migrate_adds_missing_column_to_an_old_database(tmp_path):
    """★ 老库（缺列）必须能被**补上**，而不是被重建。

      这个库是**审计资产**，"删了重建"等于把历史记录扔掉。
      所以 _migrate 只做 ADD COLUMN —— 新列必须自带默认值且非空，
      否则老库里已有的行会因为"新列是 NULL"而在读的时候炸。
    """
    import sqlite3

    from ecom_agent.store.repository import Repository

    db = tmp_path / "old.db"
    # 造一个"上一版的库"：有 runs 表、有数据、但没有新列
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_name TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '',
            duration_s REAL NOT NULL DEFAULT 0, task_fingerprint TEXT NOT NULL DEFAULT '',
            browser_use_version TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '', params_json TEXT NOT NULL DEFAULT '{}',
            start_url TEXT NOT NULL DEFAULT '', compiled_task_text TEXT NOT NULL DEFAULT '',
            guardrail_policy_json TEXT NOT NULL DEFAULT '{}', steps INTEGER NOT NULL DEFAULT 0,
            llm_json TEXT NOT NULL DEFAULT '{}', parse_status TEXT NOT NULL DEFAULT '',
            rows_collected INTEGER NOT NULL DEFAULT 0, sanity_flags_json TEXT NOT NULL DEFAULT '{}',
            result_raw TEXT NOT NULL DEFAULT '', screenshot_count INTEGER NOT NULL DEFAULT 0,
            same_frame_steps_json TEXT NOT NULL DEFAULT '[]',
            snapshot_missing_steps_json TEXT NOT NULL DEFAULT '[]',
            redaction_counts_json TEXT NOT NULL DEFAULT '{}',
            unsafe_auto_approved INTEGER NOT NULL DEFAULT 0, errors_json TEXT NOT NULL DEFAULT '[]',
            artifacts_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO runs (run_id, task_id, status) VALUES ('old-run', 'demo.books', 'completed');
        """
    )
    conn.commit()
    conn.close()

    with Repository(db) as repo:
        assert repo.list_runs()  # 老数据还在（不是被重建掉了）

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    assert "empty_selector_map_steps_json" in cols
    old = conn.execute("SELECT * FROM runs WHERE run_id='old-run'").fetchone()
    conn.close()

    assert old["task_id"] == "demo.books", "迁移把老数据弄丢了"
    assert json.loads(old["empty_selector_map_steps_json"]) == [], "老行的新列要有默认值"


# ── H. CLI ────────────────────────────────────────────────
def test_parse_params_keeps_values_as_strings():
    """★ 值保持字符串，类型转换留给编译器的 `_coerce_params`。

      在这里提前 int() 的话，参数的类型知识就分裂成两份（CLI 一份、DSL 一份），
      而两份必然漂移 —— 到时"CLI 说 limit 要是整数"和"YAML 说 limit 是 integer"
      会给出不同结论，且都认为自己是对的。
    """
    assert _parse_params(["limit=3", "keyword=书 名"]) == {"limit": "3", "keyword": "书 名"}
    assert _parse_params([]) == {}


def test_parse_params_rejects_malformed_and_does_not_guess():
    """★ 缺 `=` 直接报错而不是猜。

      `--param limit` 是想表达什么？猜"取默认值"或"设为空串"都会造出一个
      用户没要求的运行 —— 而那种运行**看起来是成功的**。
    """
    with pytest.raises(ValueError, match="名字=值"):
        _parse_params(["limit"])
    with pytest.raises(ValueError, match="名字是空的"):
        _parse_params(["=3"])


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("completed", 0),
        ("failed", 1),
        ("blocked", 3),
    ],
)
def test_exit_codes_are_a_contract(status, code):
    """★ 退出码是对外契约，不是随手写的数字。

      blocked 与 failed **必须分开**：两者的处置完全相反 ——
      failed 值得重试（换模型、放宽 max_steps），blocked 重试只会再撞一次护栏。
      CI 里一个 `if [ $? -eq 3 ]` 就能把这条区别用起来；混成 1 的话，
      "护栏拦了"会被当成"任务写错了"去查，方向从一开始就是错的。
    """
    outcome = R.RunOutcome(
        run_id="r", run_dir=Path("."), status=status, parse_status="", rows_collected=0,
        record=RunRecord(run_id="r", task_id="t"),
    )
    assert _exit_code(outcome) == code


def test_unknown_status_is_not_treated_as_success():
    """★ 未知状态按失败处理，而不是"不等于 completed 就算失败"。

      前者在加新状态时会走进这个分支并被**看见**（还有一条 stderr）；
      后者会让新状态静默地落到失败或成功里。
    """
    outcome = R.RunOutcome(
        run_id="r", run_dir=Path("."), status="weird-new-status", parse_status="",
        rows_collected=0, record=RunRecord(run_id="r", task_id="t"),
    )
    assert _exit_code(outcome) == 1


# ── I. 停机回调的 await 契约（live 验收抓到的实锤）──────────
#
# ★★ 这一节的来源不是设计，是一次真实失败的运行记录：
#
#     ❌ Result failed 1/6 times: 'bool' object can't be awaited
#     ❌ Stopping due to 5 consecutive failures
#     步数: 0    采集行数: 0
#
#   根因：`should_stop` 当时是同步方法，而库的唯一注解是
#   `Callable[[], Awaitable[bool]]`（agent/service.py:165），
#   调用点是 `if await self.register_should_stop_callback():`（1018）。
#   `await bool` 抛 TypeError，被 `_handle_step_error` 当成步进错误吞掉。
#
#   为什么离线套件当时没抓到，值得记下来：**没有任何测试碰过这个回调**。
#   它在 runner 里只是被当作一个函数对象传给 Agent，
#   而"传进去"和"库会怎么调它"是两件事。
#
#   所以下面这几条不是"补一个漏测"，是补上一条**从没被走过**的路径。


class _SyncStopInterceptor(GuardrailInterceptor):
    """把 `should_stop` 退回同步版 —— 复现那次 live 失败的确切形状。

    ★ 刻意用**子类覆盖**而不是 monkeypatch 实例属性：
      我们要验的是"这个方法的形状不对时，启动自检会不会失败"，
      而子类覆盖正是真实代码出问题时的样子（有人把 `async` 删掉就是这个）。
      monkeypatch 一个实例属性反而绕开了"方法"这层，验的东西就偏了。
    """

    def should_stop(self) -> bool:  # type: ignore[override]  # 故意写错
        return self.stop_reason is not None


def _interceptor_with_stop(tmp_path, books, cls=GuardrailInterceptor):
    return cls(
        GuardrailPolicy(books.spec.guardrails),
        None,
        recorder=make_recorder(tmp_path),
        run_id="test-run",
    )


def test_stop_callback_is_awaitable_and_returns_bool(tmp_path, books):
    """正例：我们的回调能被 await，返回值是真的 bool。"""
    interceptor = _interceptor_with_stop(tmp_path, books)
    assert asyncio.run(interceptor.verify_stop_callback()) is None
    assert asyncio.run(interceptor.should_stop()) is False, (
        "没触发硬停时必须返回 False —— 库把它当条件用，"
        "非 bool 的真值语义会让停机时机变得不可预期"
    )


def test_control_experiment_sync_stop_callback_is_caught_at_startup(tmp_path, books):
    """★★ 对照实验：同步版必须被**启动期**抓住，而不是跑到第 6 秒。

      没有这一条，上面那条"检查通过"就可能是因为别的原因通过的
      （比如 `isawaitable` 永远为真），而我们就不知道自己在验什么。
      一个永远通过的检查等于没有检查 —— 而且更坏，因为它让人以为被管住了。
    """
    interceptor = _interceptor_with_stop(tmp_path, books, cls=_SyncStopInterceptor)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(interceptor.verify_stop_callback())

    message = str(ei.value)
    assert "should_stop" in message, "报错必须点名是哪个回调"
    assert "Awaitable" in message, "要说出真正的契约：库会 await 它"
    assert "bool" in message, "要带上实际拿到的类型，否则排查的人不知道差异在哪"


def test_stop_callback_still_reports_a_stop_when_one_is_set(tmp_path, books):
    """★ 自检用的那一次 await **不能有副作用** —— 它读完就得走。

      否则"启动自检"本身会把这个拦截器变成"已经硬停过"的状态，
      于是 run 从第一步就不执行任何动作，而那看起来像护栏正常工作。
    """
    interceptor = _interceptor_with_stop(tmp_path, books)
    interceptor.stop_reason = "（测试）假装已经决定硬停"
    assert asyncio.run(interceptor.should_stop()) is True
    # 再 await 一次，值不变 —— 说明它是纯读，不是一次性消费
    assert asyncio.run(interceptor.should_stop()) is True


def test_check_wiring_actually_calls_the_stop_check(tmp_path, books):
    """★★ 证明 `check_wiring` 真的把停机自检**接上了**。

      这条比上面几条都容易漏：`verify_stop_callback` 写得再对，
      只要没有人在启动期调它，它就只是一个没人调的方法 ——
      而"没人调的正确检查"和"没有检查"在运行时的表现完全一样。

      ⚠️ 假 agent 只给 `ActionModel` 一个属性，因为 `check_wiring` 只读它
        （真值是 `self.tools.registry.create_action_model()`，service.py:783）。
        做成最小形状是刻意的：接线对 agent 的依赖面在测试里是可数的。
    """
    interceptor = _interceptor_with_stop(tmp_path, books, cls=_SyncStopInterceptor)
    agent = type("A", (), {"ActionModel": _tools().registry.create_action_model()})()

    with pytest.raises(RuntimeError, match="should_stop"):
        asyncio.run(interceptor.check_wiring(agent))


def test_check_wiring_passes_for_the_real_interceptor(tmp_path, books):
    """★ 反面对照：正常形状下 `check_wiring` 必须通过。

      两条合起来证明这个门禁是"会失败的检查"，而不是"永远失败的检查"
      —— 后者会让每次 run 都起不来，然后被人注释掉。
    """
    interceptor = _interceptor_with_stop(tmp_path, books)
    agent = type("A", (), {"ActionModel": _tools().registry.create_action_model()})()
    assert asyncio.run(interceptor.check_wiring(agent)) is None
