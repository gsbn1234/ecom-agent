"""DSL 层：加载 → 校验 → 编译。

★ 本文件里最重要的一条是 test_compiled_policy_blocks_destructive_from_real_yaml：
  它把决策矩阵从"手写 fixture"升级成"从真实 YAML 编译出来的策略"。
  在那之前，test_guardrail_policy.py 验证的是一个【可能与 YAML 不一致的副本】。
"""
import textwrap

import pytest

from ecom_agent.dsl.compiler import ParamError, compile_task, compute_fingerprint
from ecom_agent.dsl.loader import TaskLoadError, load_task, load_task_str
from ecom_agent.dsl.registry import registered, resolve_output_model
from ecom_agent.guardrails.rules import Decision

PDD = "tasks/pdd_search_products.yaml"
BOOKS = "tasks/books_demo.yaml"


@pytest.fixture(scope="module")
def pdd_spec():
    return load_task(PDD)


@pytest.fixture
def pdd(pdd_spec):
    return compile_task(pdd_spec, {"keyword": "保温杯", "limit": 5})


# ── 加载 ──────────────────────────────────────────────────
def test_both_shipped_templates_load(pdd_spec):
    assert pdd_spec.id == "pdd.search_products"
    assert load_task(BOOKS).id == "demo.books"


def test_unknown_field_error_names_the_field():
    """★ 对照实验：报错信息里必须出现【那个拼错的字段名】。

    否则测试通过的原因可能是"YAML 因为别的原因失败了"（比如缩进错），
    而我们以为验证的是 extra=forbid。断言字段名在消息里，才能确定
    是那个 validator 抓的。
    """
    bad = textwrap.dedent("""
        schema_version: "1"
        id: x
        start_url: "https://example.com"
        goal: g
        steps: ["s"]
        output_model: pdd.ProductRowList
        paginaton: {mode: none}      # ← 拼错了：paginaton
    """)
    with pytest.raises(TaskLoadError) as ei:
        load_task_str(bad)
    assert "paginaton" in str(ei.value), "报错必须点名那个字段"
    assert "pagination" in str(ei.value), "最好同时提示正确拼写"


def test_error_includes_source_path():
    with pytest.raises(TaskLoadError) as ei:
        load_task_str("id: x\n", source="tasks/x.yaml")
    assert "tasks/x.yaml" in str(ei.value)


def test_missing_file_lists_siblings():
    """★ "文件不存在"最常见的原因是名字记错了 —— 直接把候选列出来。"""
    with pytest.raises(TaskLoadError) as ei:
        load_task("tasks/does_not_exist.yaml")
    msg = str(ei.value)
    assert "不存在" in msg
    assert "pdd_search_products.yaml" in msg, "应列出同目录下现有的模板"


def test_empty_file_is_rejected():
    with pytest.raises(TaskLoadError) as ei:
        load_task_str("")
    assert "空" in str(ei.value)


def test_yaml_syntax_error_reports_line_number():
    """大段 YAML 里没有行号 = 让人自己数。"""
    with pytest.raises(TaskLoadError) as ei:
        load_task_str("id: x\n  bad indent: 1\n")
    assert "行" in str(ei.value)


def test_unsupported_schema_version():
    """★ DSL 自身版本化：报错要说清是"版本不支持"，不是"你写错了"。"""
    bad = textwrap.dedent("""
        schema_version: "99"
        id: x
        start_url: "https://e.com"
        goal: g
        steps: ["s"]
        output_model: pdd.ProductRowList
    """)
    with pytest.raises(TaskLoadError) as ei:
        load_task_str(bad)
    assert "schema_version" in str(ei.value)


def test_use_vision_true_is_rejected():
    """★ ADR-2 的强制点：无视觉模型 + 开视觉 = 一堆模型看不懂的字节。

    这个失败本身很隐蔽（表现为"模型像是没看见页面"），所以在加载期就拦。
    """
    bad = textwrap.dedent("""
        schema_version: "1"
        id: x
        start_url: "https://e.com"
        goal: g
        steps: ["s"]
        output_model: pdd.ProductRowList
        agent: {use_vision: true}
    """)
    with pytest.raises(TaskLoadError) as ei:
        load_task_str(bad)
    assert "use_vision" in str(ei.value)


# ── 参数强制 ──────────────────────────────────────────────
def test_param_coercion_and_defaults(pdd_spec):
    c = compile_task(pdd_spec, {"keyword": "杯子"})
    assert c.params["limit"] == 20, "未传则用 default"
    assert c.params["status"] == "在售中"
    assert c.params["keyword"] == "杯子"


def test_string_integer_is_coerced(pdd_spec):
    """表单提交过来的一定是字符串。归一化是编译期的职责。"""
    assert compile_task(pdd_spec, {"keyword": "x", "limit": "7"}).params["limit"] == 7


def test_bool_is_not_accepted_as_integer(pdd_spec):
    """★ isinstance(True, int) 是 True —— 不显式拦掉的话 limit=true 会静默变成 1。"""
    with pytest.raises(ParamError) as ei:
        compile_task(pdd_spec, {"keyword": "x", "limit": True})
    assert "布尔" in str(ei.value)


@pytest.mark.parametrize("bad,why", [(0, "小于 ge=1"), (201, "大于 le=200"), (-5, "负数")])
def test_range_violations_rejected(pdd_spec, bad, why):
    with pytest.raises(ParamError) as ei:
        compile_task(pdd_spec, {"keyword": "x", "limit": bad})
    assert str(bad) in str(ei.value), f"报错要带上具体的值（{why}）"


def test_enum_violation_rejected(pdd_spec):
    with pytest.raises(ParamError) as ei:
        compile_task(pdd_spec, {"keyword": "x", "status": "已售罄"})
    assert "已售罄" in str(ei.value)
    assert "在售中" in str(ei.value), "报错要列出允许值"


def test_missing_required_param(pdd_spec):
    with pytest.raises(ParamError) as ei:
        compile_task(pdd_spec, {})
    assert "keyword" in str(ei.value)


def test_unknown_param_rejected(pdd_spec):
    with pytest.raises(ParamError) as ei:
        compile_task(pdd_spec, {"keyword": "x", "limt": 5})   # 拼错了
    assert "limt" in str(ei.value)


# ── 占位符 ────────────────────────────────────────────────
def test_placeholders_are_rendered_with_param_values(pdd):
    """★★ Phase 1 验收跑出来的真实缺陷。

    原先 _render_task_text 从不替换占位符：goal 和步骤里的 {keyword}
    原样发给了 LLM，参数只出现在下面"本次参数"块里。
    于是提示词里是"按关键词「{keyword}」搜索" —— 靠下面那张清单才猜得出指代。
    """
    t = pdd.task_text
    assert "{keyword}" not in t, "占位符必须被替换，不能原样下发给 LLM"
    assert "{status}" not in t
    assert "按关键词「保温杯」搜索" in t, "替换要落在句子里，不只是清单里"
    assert "- keyword = 保温杯" in t, "替换之后清单仍保留（两者不重复，用途不同）"


def test_placeholder_in_pagination_is_also_rendered(pdd_spec):
    """★ 对照实验：只替换 goal/steps 而漏掉 pagination，是很容易发生的一半修复。

    stop_when 里就有 {limit}。若替换范围收窄到 goal+steps，
    这条断言会失败 —— 它守的是"替换范围 = spec.templated_texts()"这件事。
    """
    c = compile_task(pdd_spec, {"keyword": "x", "limit": 5})
    assert "{limit}" not in c.task_text


def test_unknown_placeholder_is_rejected_at_load_time():
    """★ 拼错的占位符是【静默失效】：{keywork} 不报错，原样变成指令文本。

    报错必须点名那个拼错的占位符，并给出正确拼写 —— 与 extra="forbid"
    处理拼错字段名的方式一致。
    """
    bad = textwrap.dedent("""
        schema_version: "1"
        id: x
        start_url: "https://e.com"
        goal: "按关键词「{keywork}」搜索"
        steps: ["搜索"]
        output_model: pdd.ProductRowList
        params:
          keyword: {type: string, required: true}
    """)
    with pytest.raises(TaskLoadError) as ei:
        load_task_str(bad)
    msg = str(ei.value)
    assert "keywork" in msg, "要点名那个拼错的占位符"
    assert "keyword" in msg, "要提示正确拼写"
    assert "goal" in msg, "要说清在哪个字段里"


def test_placeholder_for_optional_param_without_default_is_rejected():
    """★ 失败形态和拼错一模一样，所以必须一起挡。

    非必填且无默认值的参数会被 _coerce_params 填成 None，
    该占位符【同样】不会被替换 —— 静默失效，只是原因不同。
    """
    bad = textwrap.dedent("""
        schema_version: "1"
        id: x
        start_url: "https://e.com"
        goal: "搜索「{note}」"
        steps: ["s"]
        output_model: pdd.ProductRowList
        params:
          note: {type: string}
    """)
    with pytest.raises(TaskLoadError) as ei:
        load_task_str(bad)
    assert "required" in str(ei.value) and "default" in str(ei.value), "报错要给出两条出路"


def test_regex_quantifier_in_guardrail_is_not_mistaken_for_a_placeholder():
    """★★ 对照实验：证明占位符扫描【必须】只扫人类可读文本，不能扫护栏正则。

    正则里合法的量词恰好长得像占位符：`试用{2}` 的 `{2}` 完全符合 PLACEHOLDER_RE。
    如果为了"更保险"去扫全 spec，这条完全正确的规则会被判成未知占位符 ——
    护栏规则写不出来，而报错还指向一个不存在的语法问题。
    """
    ok = textwrap.dedent("""
        schema_version: "1"
        id: x
        start_url: "https://e.com"
        goal: "什么也不做"
        steps: ["s"]
        output_model: pdd.ProductRowList
        guardrails:
          rules:
            - id: r1
              decision: block
              match_element_text: "试用{2}|限时{1,3}次"
    """)
    spec = load_task_str(ok)   # ← 不该抛
    assert spec.guardrails.rules[0].match_element_text == "试用{2}|限时{1,3}次"


# ── 编译产物 ──────────────────────────────────────────────
def test_task_text_contains_everything_it_should(pdd):
    t = pdd.task_text
    assert "保温杯" in t, "参数要落进文本"
    assert "最多翻 3 页" in t, "分页上限要落进文本"
    assert "系统护栏" in t, "护栏条款要落进文本（已批准计划的设计）"
    assert "1." in t and "2." in t, "步骤要编号"
    assert "需要人工登录" in t or "登录" in t, "requires_login 要插入停止指令"


def test_task_text_does_not_embed_json_schema(pdd):
    """★★ 不要在 task_text 里自己拼 JSON schema。

    Agent.__init__ 会通过 _enhance_task_with_schema 自己拼一遍。
    自己再拼会让 schema 在提示词里出现两次 —— 既浪费 token，
    又制造了"两处 schema 版本不一致"的可能（只改了一处的那种）。

    这里用 schema 的典型标记来断言它没被提前拼进去。
    """
    t = pdd.task_text
    assert "$defs" not in t and '"properties"' not in t, "schema 不该由我们拼进 task_text"


def test_task_text_says_blocked_ops_are_blocked_not_discouraged(pdd):
    """护栏条款的措辞必须是"会被系统拒绝"，不是"请不要"。

    前者 LLM 会绕开，后者 LLM 会试图礼貌地请求许可 —— 而请求许可这个动作
    本身也要烧一步 token。
    """
    t = pdd.task_text
    assert "会被系统直接拒绝" in t
    assert "会暂停并请求人工确认" in t


def test_browser_kwargs_keeps_browser_alive(pdd):
    """★ keep_alive=True 是硬要求。

    run() 默认结束时 kill 浏览器并 await self.close()（service.py:2726）。
    护栏的回退方案 A（停机→审批→续跑）必须在第二次 run 时还能拿到活着的浏览器，
    否则第二次 run 第一句话就是"浏览器已经死了" —— 而那个报错完全指不到 keep_alive。
    """
    assert pdd.browser_kwargs["keep_alive"] is True
    assert pdd.browser_kwargs["allowed_domains"] == ["mms.pinduoduo.com", "*.pinduoduo.com"]


def test_max_steps_goes_to_run_not_agent(pdd):
    """★★ max_steps 在 run() 上，不在 Agent 构造函数上。

    写错地方【不会报错】，只会静默地按默认步数跑 —— 于是这个"保险丝"根本没接上，
    而一切看起来都正常。这条断言就是防止它被挪回构造函数。
    """
    assert pdd.run_kwargs["max_steps"] == 60
    assert "max_steps" not in pdd.agent_kwargs


def test_chrome_path_only_passed_when_nonempty(pdd_spec):
    """★ 传空串会让 browser-use 去找一个名为 "" 的文件。"""
    assert "executable_path" not in compile_task(pdd_spec, {"keyword": "x"}).browser_kwargs
    c = compile_task(pdd_spec, {"keyword": "x"}, chrome_path="/tmp/chrome")
    assert c.browser_kwargs["executable_path"] == "/tmp/chrome"


def test_user_data_dir_only_passed_when_nonempty(pdd_spec):
    """★★ 持久 profile 只在**显式给**的时候才传，而默认必须是"不传"。

    两条理由，各对应下面的一个断言：
      1. 传空串是同一个坑（库会拿 "" 当目录用）—— 所以空的时候这个键必须**不存在**；
      2. 更要紧的是语义：不传 = 每次临时 profile（无状态、可复现），
         传了 = 复用带 cookie 的目录（有状态）。默认必须是无状态的那个 ——
         否则 CI 里 11 条浏览器用例会共用一个 cookie 目录，测试之间互相污染，
         而且只在特定执行顺序下才暴露。

    ★ 两个断言缺一不可：只断言"默认没有"的话，**把整个 kwarg 删掉也能通过**。
      存在的那一半才是对照。
    """
    assert "user_data_dir" not in compile_task(pdd_spec, {"keyword": "x"}).browser_kwargs
    assert (
        "user_data_dir" not in compile_task(pdd_spec, {"keyword": "x"}, user_data_dir="").browser_kwargs
    )

    c = compile_task(pdd_spec, {"keyword": "x"}, user_data_dir="/tmp/prof")
    assert c.browser_kwargs["user_data_dir"] == "/tmp/prof"


def test_output_model_resolved(pdd):
    assert pdd.output_model is resolve_output_model("pdd.ProductRowList", 1)
    assert "pdd.ProductRowList" in registered()


# ── 指纹 ──────────────────────────────────────────────────
def test_fingerprint_stable_regardless_of_param_order(pdd_spec):
    a = compute_fingerprint(pdd_spec, {"keyword": "x", "limit": 1, "status": "全部"})
    b = compute_fingerprint(pdd_spec, {"status": "全部", "limit": 1, "keyword": "x"})
    assert a == b, "字典序不能影响指纹，否则指纹失去『同一件事』的判定能力"


def test_fingerprint_changes_with_params(pdd_spec):
    assert compute_fingerprint(pdd_spec, {"keyword": "x"}) != compute_fingerprint(pdd_spec, {"keyword": "y"})


def test_fingerprint_changes_when_guardrails_change(pdd_spec):
    """★★ 指纹必须覆盖护栏 —— 这是最危险的一类漂移。

    指纹相同而策略不同时，回放会用【当前】策略去解释一次按【旧】策略执行的记录，
    于是报告里显示的"当时为什么放行"是错的。审计信息错了比没有更糟。
    """
    original = pdd_spec.guardrails.model_dump()
    mutated = {**original, "rules": [dict(r, decision="allow") for r in original["rules"]]}
    changed = pdd_spec.model_copy(update={"guardrails": pdd_spec.guardrails.model_validate(mutated)})

    assert compute_fingerprint(pdd_spec, {}) != compute_fingerprint(changed, {})


# ── 真正的验收：从 YAML 编译出的策略跑决策矩阵 ─────────────
def test_compiled_policy_blocks_destructive_from_real_yaml(pdd):
    """★★ Phase 1 的验收标准。

    与 test_guardrail_policy.py 的区别：那里的 spec 是【手写 fixture】，
    可能与 YAML 不一致；这里是从 tasks/pdd_search_products.yaml 真编译出来的。
    只有这条测试能证明"YAML 里写的护栏真的会生效"。
    """
    url = "https://mms.pinduoduo.com/goods/goods_list"

    blocked = pdd.policy.evaluate("click", {}, url, "批量删除")
    assert blocked.decision is Decision.BLOCK
    assert blocked.rule_id == "block-destructive"

    confirm = pdd.policy.evaluate("click", {}, url, "立即支付")
    assert confirm.decision is Decision.CONFIRM

    allowed = pdd.policy.evaluate("click", {}, url, "搜索")
    assert allowed.decision is Decision.ALLOW

    # 白名单来自 YAML：站外导航必须被拦
    assert pdd.policy.check_navigation("https://www.taobao.com/").decision is Decision.BLOCK
    assert pdd.policy.check_navigation(url).decision is Decision.ALLOW


def test_compiled_policy_order_independent_after_yaml_roundtrip(pdd_spec):
    """★ 顺序无关这条性质，在【经过 YAML 序列化往返】之后仍然成立。

    手写 fixture 里顺序是可控的；YAML 往返会重排/规范化，
    所以这条性质必须在真实路径上再验一次。
    """
    from ecom_agent.guardrails.policy import GuardrailPolicy
    from ecom_agent.guardrails.rules import GuardrailSpec

    forward = GuardrailPolicy(pdd_spec.guardrails)
    flipped = pdd_spec.guardrails.model_copy(update={"rules": list(reversed(pdd_spec.guardrails.rules))})
    backward = GuardrailPolicy(GuardrailSpec(**flipped.model_dump()))

    url = "https://mms.pinduoduo.com/goods/goods_list"
    for action, text in [("click", "删除并搜索"), ("click", "立即支付"), ("click", "搜索"), ("click", "编辑")]:
        assert forward.evaluate(action, {}, url, text).decision is backward.evaluate(action, {}, url, text).decision


def test_describe_is_printable(pdd):
    d = pdd.describe()
    assert "pdd.search_products" in d
    assert "护栏条款" in d
