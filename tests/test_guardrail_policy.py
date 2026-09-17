"""护栏决策矩阵。

★ 本文件是 Phase 1 的交付物之一：一份把「什么动作会被怎么处置」钉死的真值表。
  它同时也是文档 —— 想知道某条规则的边界行为，读这里比读 YAML 快。

★ 每条断言都尽量配一个对照实验（本项目的招牌手法）：
  断言的输入必须【确实含有】被检测的特征，否则当规则因为别的原因失效时，
  测试依然会通过（"没检出它"和"它本来就不在里面"结果一样）。
"""
import json

import pytest

from ecom_agent.guardrails.policy import GuardrailPolicy
from ecom_agent.guardrails.rules import Decision, GuardrailRule, GuardrailSpec

PDD_URL = "https://mms.pinduoduo.com/goods/goods_list"


@pytest.fixture
def spec() -> GuardrailSpec:
    """规则集与 `tasks/pdd_search_products.yaml` 的 guardrails 段保持一致。

    ★ 这两处必须同步：YAML 改了而这里没改，测试就在验证一个不存在的策略。
      完整的真值表已经有一条打在**真 YAML** 上
      （`test_runner_offline.py::test_pdd_template_decisions_after_the_fix`），
      所以这里这份副本的职责只剩"测引擎本身"（最严优先、顺序无关、正则语义）。
      但**判据**仍然必须同形 —— 否则引擎层的测试就与实际规则脱节了。

    ⚠️ 2026-09-17 更新：pdd 模板把 `allow-readonly` 拆成了两条
      （`allow-readonly-text` 带元素文本判据 / `allow-readonly-noelement` 不带），
      并新增了 `block-destructive-keys` / `allow-readonly-keys`。
      原因是一个静默死条目：带 `match_element_text` 的规则**永远匹配不上**
      不针对元素的动作（`extract` / `go_back` / `extract_table` / `send_keys`）。
      完整理由见 `tasks/pdd_search_products.yaml` 的长注释。
    """
    return GuardrailSpec(
        allowed_domains=["mms.pinduoduo.com", "*.pinduoduo.com"],
        prohibited_domains=["*.taobao.com", "*.jd.com", "*.alibaba.com"],
        default_decision=Decision.CONFIRM,
        rules=[
            GuardrailRule(
                id="block-destructive",
                match_action=["click", "input"],
                match_element_text="删除|批量删除|下架|清空|重置|退出登录|解绑|注销",
                decision=Decision.BLOCK,
                reason="破坏性/不可逆操作，会改店铺真实数据",
            ),
            GuardrailRule(
                id="block-account",
                match_action=["navigate", "click"],
                match_param_regex="logout|signout|unbind|password|setting",
                decision=Decision.BLOCK,
                reason="账号/会话相关，agent 不应触碰",
            ),
            GuardrailRule(
                id="confirm-money",
                match_action=["click"],
                match_element_text="提交订单|立即支付|确认付款|打款|提现|开通|购买|续费|充值",
                decision=Decision.CONFIRM,
                reason="资金相关，必须人工二次确认",
            ),
            GuardrailRule(
                id="confirm-publish",
                match_action=["click"],
                match_element_text="发布商品|保存并上架|提交审核|确认提交",
                decision=Decision.CONFIRM,
                reason="会真实创建或上线商品",
            ),
            GuardrailRule(
                id="allow-readonly-text",
                match_action=["input", "click", "scroll", "dropdown_options", "select_dropdown"],
                match_url="*mms.pinduoduo.com*",
                match_element_text="搜索|查询|筛选|确定|取消|下一页|上一页|关闭",
                decision=Decision.ALLOW,
                reason="只读检索类操作（可判定元素文本的那些）",
            ),
            GuardrailRule(
                id="allow-readonly-noelement",
                match_action=["extract", "extract_table", "go_back", "scroll", "find_elements", "find_text"],
                match_url="*mms.pinduoduo.com*",
                decision=Decision.ALLOW,
                reason="不针对元素的只读动作：取数据、翻历史、翻页、找元素",
            ),
        ],
    )


# ── 真值表 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "action,element_text,expected,why",
    [
        # 破坏性 → BLOCK
        ("click", "批量删除", Decision.BLOCK, "批量删除是不可逆的"),
        ("click", "删除", Decision.BLOCK, "单条删除同样是破坏性"),
        ("click", "下架", Decision.BLOCK, "下架会改店铺真实在售状态"),
        ("click", "退出登录", Decision.BLOCK, "账号相关"),
        ("click", "重置", Decision.BLOCK, "重置会清掉用户已填内容"),
        # 资金/创建 → CONFIRM
        ("click", "立即支付", Decision.CONFIRM, "资金相关"),
        ("click", "提交订单", Decision.CONFIRM, "资金相关"),
        ("click", "发布商品", Decision.CONFIRM, "会真实创建商品"),
        ("click", "确认提交", Decision.CONFIRM, "会真实提交"),
        # 只读 → ALLOW
        ("click", "搜索", Decision.ALLOW, "只读检索"),
        ("click", "下一页", Decision.ALLOW, "翻页是只读的"),
        ("input", "搜索", Decision.ALLOW, "只读检索"),
        ("scroll", "下一页", Decision.ALLOW, "滚动是只读的"),
        # 未命中任何规则 → default_decision
        ("click", "编辑", Decision.CONFIRM, "未命中 → 默认 confirm（不是 allow）"),
        ("click", "随便什么按钮", Decision.CONFIRM, "未知动作默认要人确认"),
        # ── 不针对元素的动作：走 allow-readonly-noelement（不带文本判据的那条）──
        #   ★ 这几行是 2026-09-17 那次修复的可执行证据。修复前它们全都是 CONFIRM ——
        #     因为旧写法把 extract / go_back 塞进了**带** match_element_text 的规则里，
        #     而那条规则对无元素动作**永不命中**。
        #     ⚠️ 注意"永不命中"的后果是落到 default_decision，**不是**被拦死：
        #     一个是"没人管"，一个是"要人点批准"，在报告里长得完全不同。
        ("extract", None, Decision.ALLOW, "取数据是只读动作，不需要元素文本判据"),
        ("extract_table", None, Decision.ALLOW, "同上：它的参数是 table_index/max_rows"),
        ("go_back", None, Decision.ALLOW, "回历史是只读的"),
        ("scroll", None, Decision.ALLOW, "不带 index 的滚动 → 落到无元素动作那条放行规则"),
        # ── 对照：刻意**不**放行的两个 ──
        #   没有这两行的话，一个"凡无元素动作一律放行"的实现也能让上面全绿。
        ("evaluate", None, Decision.CONFIRM, "★ 能执行任意 JS —— 不是只读动作"),
        ("navigate", None, Decision.CONFIRM, "★ 导航归 Layer 0 白名单管，这里不重复放行"),
    ],
)
def test_decision_matrix(spec, action, element_text, expected, why):
    p = GuardrailPolicy(spec)
    got = p.evaluate(action, {}, PDD_URL, element_text).decision
    assert got is expected, f"{action} + {element_text!r} 应为 {expected.value}（{why}），实得 {got.value}"


def test_default_decision_is_confirm_not_allow(spec):
    """★ ADR-7 的核心断言。

    LLM 的失败模式是【做了你没预料到的那个动作】—— 而"没写规则的地方"
    正是它最可能乱来的地方。默认放行等于护栏只在写了规则处生效。
    """
    assert spec.default_decision is Decision.CONFIRM
    r = GuardrailPolicy(spec).evaluate("click", {}, PDD_URL, "一个没人写规则的新按钮")
    assert r.decision is Decision.CONFIRM
    assert r.rule_id is None, "未命中规则时不应有 rule_id"


# ── 最严优先 & 顺序无关 ───────────────────────────────────
def test_most_severe_wins_when_multiple_rules_match(spec):
    """allow 和 block 同时命中时，判 block。

    构造："删除并搜索" 同时匹配 block-destructive（删除）和
    allow-readonly-text（搜索）。
    """
    r = GuardrailPolicy(spec).evaluate("click", {}, PDD_URL, "删除并搜索")
    assert "block-destructive" in r.matched_rule_ids, "前提：两个规则都该命中"
    assert "allow-readonly-text" in r.matched_rule_ids, "前提：两个规则都该命中"
    assert r.decision is Decision.BLOCK
    assert r.rule_id == "block-destructive"


def test_rule_order_does_not_affect_decision(spec):
    """★★ 本项目护栏最重要的性质：决策与 YAML 书写顺序无关。

    为什么这条值得单独一个测试：如果聚合语义是"先匹配先赢"，那么
    「把 allow 规则写在 block 规则前面」就是一个【静默的安全漏洞】——
    功能测试全过，review 的人从 YAML 上也看不出问题，因为两条规则各自都对。

    做法：同一组规则，正序和逆序各建一个 policy，断言在【一整批输入】上决策完全一致。
    ★ 不能只测一个输入 —— 顺序 bug 往往只在特定规则组合上暴露。
    ★ 也要断言 rule_id 一致：报告里记录的是哪条规则，同样不能随顺序漂。
    """
    forward = GuardrailPolicy(GuardrailSpec(**{**spec.model_dump(), "rules": list(spec.rules)}))
    reversed_ = GuardrailPolicy(
        GuardrailSpec(**{**spec.model_dump(), "rules": list(reversed(spec.rules))})
    )

    cases = [
        ("click", "删除并搜索", PDD_URL),
        ("click", "立即支付", PDD_URL),
        ("click", "搜索", PDD_URL),
        ("click", "编辑", PDD_URL),
        ("input", "下架并查询", PDD_URL),
        ("navigate", "", PDD_URL),
        ("click", "取消", PDD_URL),
    ]
    for action, text, url in cases:
        a = forward.evaluate(action, {}, url, text)
        b = reversed_.evaluate(action, {}, url, text)
        assert a.decision is b.decision, f"{action}+{text!r} 顺序影响了决策：{a.decision} vs {b.decision}"
        assert a.rule_id == b.rule_id, f"{action}+{text!r} 顺序影响了 rule_id：{a.rule_id} vs {b.rule_id}"


def test_ties_broken_by_rule_id_not_yaml_order():
    """同级规则命中时，取 rule_id 字典序最小的那条 —— 而不是 YAML 里靠前的那条。

    这样报告里记录的 rule_id 在任何顺序下都稳定，追溯时不会看着像行为变了。
    """
    a = GuardrailRule(id="zzz-later", match_action=["click"], decision=Decision.BLOCK, reason="z")
    b = GuardrailRule(id="aaa-earlier", match_action=["click"], decision=Decision.BLOCK, reason="a")
    for rules in ([a, b], [b, a]):
        r = GuardrailPolicy(GuardrailSpec(rules=rules)).evaluate("click", {}, "", None)
        assert r.rule_id == "aaa-earlier"
        assert r.matched_rule_ids == ("aaa-earlier", "zzz-later")


# ── fail-closed：拿不到元素文本时不能"当作空串继续匹配" ────
def test_missing_element_text_does_not_match_text_rules(spec):
    """★ 对照实验。

    如果实现里把 element_text=None 当作空串处理，那么 match_element_text 的规则
    在 None 输入下会走向哪个分支就取决于正则写的是什么 —— 空正则会匹配一切。
    这里断言：None 时文本类规则【不命中】。

    对照：同样的动作 + 文本 "批量删除" → 必须命中。两条一起才说明
    "不命中"是因为 None 被正确拒绝，而不是因为规则本身坏了。
    """
    p = GuardrailPolicy(spec)
    negative = p.evaluate("click", {}, PDD_URL, None)
    positive = p.evaluate("click", {}, PDD_URL, "批量删除")

    assert "block-destructive" not in negative.matched_rule_ids
    assert "block-destructive" in positive.matched_rule_ids, "对照：有文本时必须命中"


def test_empty_regex_is_rejected_at_load_time():
    """★ 空正则必须在加载期拦掉 —— 它是个方向完全反了的坑。

      re.compile("") 是【合法】的，而且匹配一切。所以一条本想"只拦特定文本"的规则
      会变成"拦死所有操作"。而且它不以报错形式出现，只表现为
      "所有操作都要人工确认"，人看到的现象是"护栏好像有点烦"，不是"配置写错了"。

      对照实验：'.+'（显式表达"任意非空文本"）必须被接受 ——
      证明拦的是"空串"这件事本身，不是"想匹配任意文本"这个意图。
    """
    with pytest.raises(ValueError) as ei:
        GuardrailRule(id="empty-text", match_action=["click"], match_element_text="", decision=Decision.BLOCK)
    assert "空串" in str(ei.value)

    # 对照：显式写法必须放行
    ok = GuardrailRule(id="any-text", match_action=["click"], match_element_text=".+", decision=Decision.BLOCK)
    p = GuardrailPolicy(GuardrailSpec(rules=[ok]))
    assert p.evaluate("click", {}, PDD_URL, "任意文本").decision is Decision.BLOCK
    assert p.evaluate("click", {}, PDD_URL, None).decision is not Decision.BLOCK, "空文本仍不该命中"


def test_regex_invalid_is_rejected_at_load_time():
    """★ 正则写错必须在【加载期】报错，不能拖到运行期。

    运行期发现正则写错，是在浏览器已经导航过去、LLM 已经下了指令之后。
    那时错误表现为"某条规则莫名其妙没生效"，根因（打错的括号）藏在 YAML 里，
    中间隔着好几层调用栈。
    """
    with pytest.raises(ValueError) as ei:
        GuardrailRule(id="bad", match_element_text="删除(未闭合", decision=Decision.BLOCK)
    assert "正则无法编译" in str(ei.value)
    assert "删除(未闭合" in str(ei.value), "报错信息要带上原文，否则还得回去翻 YAML"


# ── 加载期结构校验 ────────────────────────────────────────
def test_duplicate_rule_ids_rejected():
    r = GuardrailRule(id="same", match_action=["click"], decision=Decision.BLOCK)
    with pytest.raises(ValueError) as ei:
        GuardrailSpec(rules=[r, r])
    assert "same" in str(ei.value)


def test_more_than_100_domains_rejected():
    """★ 上游硬限制：合计 >100 条会让通配符【静默失效】。

    最恶劣的一类失败：配置写得越多护栏反而越弱，且没有任何警告。
    """
    with pytest.raises(ValueError) as ei:
        GuardrailSpec(allowed_domains=[f"a{i}.com" for i in range(60)],
                      prohibited_domains=[f"b{i}.com" for i in range(60)])
    assert "100" in str(ei.value)


def test_100_domains_is_still_ok():
    """边界对照：正好 100 条不该被拒。"""
    GuardrailSpec(allowed_domains=[f"a{i}.com" for i in range(50)],
                  prohibited_domains=[f"b{i}.com" for i in range(50)])


def test_unknown_yaml_key_rejected():
    """extra=forbid：拼错字段名必须报错。"""
    with pytest.raises(ValueError) as ei:
        GuardrailRule(id="x", decision=Decision.BLOCK, match_elemen_text="删除")  # 少了个 t
    assert "match_elemen_text" in str(ei.value)


# ── 导航前置校验 ──────────────────────────────────────────
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://mms.pinduoduo.com/goods/goods_list", Decision.ALLOW),
        ("https://www.pinduoduo.com/", Decision.ALLOW),
        ("https://other.pinduoduo.com/x", Decision.ALLOW),
        ("https://www.taobao.com/", Decision.BLOCK),
        ("https://item.jd.com/1.html", Decision.BLOCK),
        ("https://www.alibaba.com/", Decision.BLOCK),
        ("https://example.com/", Decision.BLOCK),
        ("about:blank", Decision.BLOCK),
        ("", Decision.BLOCK),
    ],
)
def test_check_navigation(spec, url, expected):
    """★ 配了白名单但没命中 → 一律拦，【不看 default_decision】。

    白名单的语义是"只允许这些"。回落到 confirm 会让它变成"建议清单"，
    而浏览器导航是不可逆的：一旦过去，页面上的脚本已经执行了。
    """
    assert GuardrailPolicy(spec).check_navigation(url).decision is expected


def test_prohibited_beats_allowed():
    """例外必须能覆盖通配：allowed 常有宽通配（*.pinduoduo.com），
    prohibited 是精确的例外清单。两者都命中时判 block。
    """
    s = GuardrailSpec(allowed_domains=["*.example.com"],
                      prohibited_domains=["admin.example.com"])
    assert GuardrailPolicy(s).check_navigation("https://admin.example.com/x").decision is Decision.BLOCK
    assert GuardrailPolicy(s).check_navigation("https://shop.example.com/x").decision is Decision.ALLOW


# ── 连续被拦计数 ──────────────────────────────────────────
def test_block_streak_trips_at_limit():
    p = GuardrailPolicy(GuardrailSpec(max_consecutive_blocks=3))
    assert p.record_block() is False
    assert p.record_block() is False
    assert p.record_block() is True, "第 3 次应触发硬停"


def test_block_streak_resets_on_unblocked_action():
    """★ 归零的时机是"未被拦"，不是"执行成功"。

    我们要数的是"LLM 连续撞护栏"这件事。执行成不成功是另一回事 ——
    LLM 可能在反复尝试同一个被拒的动作，每次都以不同方式失败。
    """
    p = GuardrailPolicy(GuardrailSpec(max_consecutive_blocks=3))
    p.record_block()
    p.record_block()
    p.record_success()
    assert p.block_streak == 0
    assert p.record_block() is False, "归零后不该立刻触发"
    assert p.record_block() is False
    assert p.record_block() is True


# ── param_regex 的能力与边界 ──────────────────────────────
def test_param_regex_matches_url_field(spec):
    p = GuardrailPolicy(spec)
    assert p.evaluate("navigate", {"url": "https://x/logout"}, PDD_URL, None).decision is Decision.BLOCK


def test_json_blob_does_not_concatenate_across_fields():
    """★★ 对照实验，用来钉死一个我一开始想当然写错的假设。

      直觉上"把整个 params 序列化后做正则"应该能检出【跨字段拼接】出来的词。
      实测不能：json.dumps({"a":"sig","b":"nout"}) 得到 '{"a": "sig", "b": "nout"}'，
      中间的 JSON 分隔符把 "signout" 打断了，blob 里不含这个子串。

      所以 rules.py 里 match_param_regex 的注释【不能】宣称"能检出跨字段特征"。
      这条测试的作用就是防止那个说法在某次重构里被想当然地写回去 ——
      一个写在注释里的错误能力声明，比没有注释更危险。
    """
    blob = json.dumps({"a": "sig", "b": "nout"}, ensure_ascii=False, sort_keys=True, default=str)
    assert "signout" not in blob, "json.dumps 的行为变了，rules.py 的注释需要重新评估"


def test_param_regex_can_constrain_key_and_value_together():
    """★ 这才是 json.dumps 方案真正独有的能力：正则同时约束【键】和【值】。

      "参数名叫 goods_id 且值是 6 位以上数字"这种规则，遍历各字段的值分别匹配
      是表达不出来的 —— 值本身没有名字。

      两个对照实验一起才说明正则确实同时作用在键和值上：
        对照 1：值变短 → 不命中（证明约束作用在值上）
        对照 2：值不变但键名换掉 → 不命中（证明约束作用在键上）
    """
    rule = GuardrailRule(
        id="long-goods-id",
        match_action=["click"],
        match_param_regex=r'"goods_id":\s*"\d{6,}"',
        decision=Decision.BLOCK,
        reason="长商品ID",
    )
    p = GuardrailPolicy(GuardrailSpec(rules=[rule]))

    assert p.evaluate("click", {"goods_id": "123456789"}, "", None).decision is Decision.BLOCK
    assert p.evaluate("click", {"goods_id": "12"}, "", None).decision is not Decision.BLOCK, "对照1：值不够长"
    assert (
        p.evaluate("click", {"other_id": "123456789"}, "", None).decision is not Decision.BLOCK
    ), "对照2：键名不对，即使值符合也不该命中"
