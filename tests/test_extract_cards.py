"""`extract_cards` 的契约、字段判据、以及三种失败形态。

★ 这里**测不到**什么，先说清楚（与 test_extract_table.py 同一条纪律）：
  `_CARDS_JS` 那段脚本的正确性 —— "同父同形兄弟"这条判据在真 DOM 上灵不灵 ——
  单测**测不了**。假 page 只会把我喂进去的 payload 原样吐回来。
  它由两样东西保证：
    · S8 探路时在真站点上跑过一次（35 张卡，见 docs/spikes.md）；
    · `tests/test_extract_cards_dom.py` 在合成 DOM 上的 e2e —— 那一份**在真浏览器里**
      执行 `_CARDS_JS`，含一条对照组：页面上 6 个 `<script>` 比 4 张卡还多，
      所以"选中的组是 DIV 而不是 SCRIPT"只在 SKIP 过滤真的生效时成立。
  写下这条是为了不让"单测全绿"被读成"结构判据是对的"——
  下面这条 `test_script_skips_non_rendered_tags` 只能证明**字符串写在脚本里**，
  证明不了它在真 DOM 上真的把脚本挡住了（那件事在 DOM 那个文件里）。

★ 这个文件里最值钱的几条是**对照实验**（拆掉机制 → 对应那条必须红）：
  · 拆掉结构过滤（不过滤不渲染的标签）→ 那条"埋点脚本被当成列表"的用例红；
  · 让字段判据永远匹配不上 → missing 计数那条红（而不是静默给空串）；
  · 让闭包不接（`fields=[]`）→ 那条"说清为什么拒绝"的用例红。
"""
from __future__ import annotations

import json

import pytest
from browser_use import ActionResult, Tools

import ecom_agent.actions.extract_cards as M
from ecom_agent.actions import (
    build_tools,
    verify_extract_cards_round_trip,
)
from ecom_agent.dsl.models import CardField, TaskSpec
from ecom_agent.dsl.loader import load_task_str


# ── 桩：只回答 get_current_page()，不碰浏览器 ────────────────
class _FakePage:
    """★★ `evaluate` **必须返回字符串** —— 与 test_extract_table.py 里那个桩同一条理由。

    真实对象是 `browser_use.actor.page.Page`，它的 `evaluate(...) -> str` 永远
    返回字符串（对象走 json.dumps）。桩要是直接回 dict，"生产代码忘了 json.loads"
    在单测里完全看不出来。桩的形状比真实对象宽松时，它就不再是桩。
    """

    def __init__(self, *, payload=None, raw=None, exc=None) -> None:
        self.payload = payload
        self.raw = raw
        self.exc = exc
        self.calls: list[tuple[str, object]] = []

    async def evaluate(self, script, arg):
        self.calls.append((script, arg))
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            return self.raw
        return json.dumps(self.payload)


class _FakeSession:
    def __init__(self, page) -> None:
        self._page = page

    async def get_current_page(self):
        return self._page


def _cards_payload(cards, *, truncated=False, inventory=None, total=None):
    """造一份与 `_CARDS_JS` 返回结构一致的 payload。"""
    return {
        "found": True,
        "groupCount": 1,
        "inventory": inventory
        if inventory is not None
        else [{"tag": "SPAN", "memberCount": len(cards), "sample": "样例"}],
        "pickedTag": "SPAN",
        "pickedMembers": len(cards),
        "cards": cards,
        "totalCards": total if total is not None else len(cards),
        "truncated": truncated,
    }


# 与 S8 在真站点上看到的卡片形状一致：四行 —— 标题 / ¥价格 / 热度 / 按钮。
# ⚠️ 内容是**编的**：真实商品标题不进仓库（README 已声明演示数据全部来自本地 mock）。
CARD = ["测试商品甲", "¥12.30", "热度 88", "发布同款"]

FIELDS = [
    CardField(name="title", pattern=r"^(?!¥|热度|发布同款).+"),
    CardField(name="price", pattern=r"^¥\s*([\d.]+)"),
    CardField(name="heat", pattern=r"热度\s*(\d+)"),
]


async def _run(fields=FIELDS, payload=None, *, raw=None, exc=None, **params):
    """跑一次实现体。

    ★ `raw` / `exc` 必须是**具名**的：它们属于页桩，不属于动作参数。
      早先写成 `**params` 一起吃进去，于是 `ExtractCardsAction(raw=...)`
      被 pydantic 的 extra="forbid" 挡下 —— 那是**好事**（红线那两个字段
      就是这么被挡住的），但它让"返回值读不懂"这类用例根本跑不到被测代码。
    """
    page = _FakePage(
        payload=payload if payload is not None else _cards_payload([CARD]),
        raw=raw,
        exc=exc,
    )
    action = M.ExtractCardsAction(**params)
    result = await M.extract_cards_impl(action, _FakeSession(page), fields=fields)
    return result, page


# ── 注册期契约 ──────────────────────────────────────────────
def test_extract_cards_is_registered_by_function_name():
    """动作名 = **函数名**。

    ★ 钉的是"改名字"的后果：把内层函数改名（比如为"避免遮蔽模块级常量"），
      动作名跟着变，而 YAML 的 `match_action: [..., extract_cards, ...]`
      和 TaskSpec 里那条"步骤必须提到 extract_cards"的校验都会**静默失效**。
    """
    tools = build_tools()
    assert M.CARDS_ACTION in tools.registry.registry.actions
    assert M.CARDS_ACTION == "extract_cards"


def test_browser_session_is_not_exposed_to_the_llm():
    """★ 红线相邻：保留参数名不能出现在给 LLM 的 schema 里。

    和 `extract_table` 同一条：库会把 `SpecialActionParameters` 的 9 个字段名
    从 tool schema 里整个剥掉。它出现了就意味着"库注入"变成了"让模型填一个
    BrowserSession 对象"——模型填不出来，于是这个动作永远调不动。
    """
    tools = build_tools()
    schema = tools.registry.registry.actions[M.CARDS_ACTION].param_model.model_json_schema()
    assert "browser_session" not in schema.get("properties", {})
    # 参数模型的字段就是这三个，不多不少（多了就是某个字段被暴露给 LLM 了）
    assert set(schema["properties"]) == {"group_index", "min_members", "max_cards"}


def test_fields_are_not_an_action_parameter():
    """★★ 字段判据**不是**给 LLM 填的参数 —— 这条是整条设计的支点。

    判据一旦进了参数模型，模型就能改"哪一行算价格"，而价格被转录正是本项目
    拒绝的事。它只能走闭包（引擎事实 3）。
    """
    tools = build_tools()
    schema = tools.registry.registry.actions[M.CARDS_ACTION].param_model.model_json_schema()
    assert "fields" not in schema.get("properties", {})
    assert "card_fields" not in schema.get("properties", {})


def test_the_action_description_names_the_declared_fields():
    """动作描述里要报出本任务声明了哪些字段。

    ★ 为什么这条值得钉：LLM 是靠工具描述决定"用哪个动作、传什么参数"的。
      描述里不说字段名，模型就没有依据判断"这一页该用 extract_cards"；
      而它一旦改用库内置的 `extract`，价格就被转录了 —— 失效静默且合法。
    """
    tools = build_tools(card_fields=[CardField(name="标价", pattern=r"\d+")])
    desc = tools.registry.registry.actions[M.CARDS_ACTION].description
    assert "标价" in desc


def test_build_tools_without_card_fields_still_registers():
    """没有 card_fields 的任务照常注册 —— 绝大多数任务不用这个动作。

    ★ 对照点：注册**成功**不代表可用。可用性由 YAML 声明（见下面
      "没配判据时说清楚"那条用例）。
    """
    tools = build_tools()
    assert M.CARDS_ACTION in tools.registry.registry.actions


# ── 字段判据 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fields_are_matched_line_by_line_and_first_hit_wins():
    """逐行匹配 + 同一字段取**第一个命中**的行。"""
    result, _ = await _run(payload=_cards_payload([CARD]))
    row = json.loads(result.extracted_content)["rows"][0]
    assert row == {"title": "测试商品甲", "price": "12.30", "heat": "88"}


@pytest.mark.asyncio
async def test_capture_group_wins_over_the_whole_match():
    """有捕获组取第 1 组，没有就取整个匹配 —— 这条规矩写在 CardField 的说明里。"""
    result, _ = await _run(payload=_cards_payload([["原价 ¥99.00 现价 ¥12.30"]]))
    row = json.loads(result.extracted_content)["rows"][0]
    # price 的判据是 ^¥... —— 这一行以"原价"开头，所以它**不该**命中（行首锚定）
    assert row["price"] == ""


@pytest.mark.asyncio
async def test_a_field_that_matches_nothing_is_empty_and_counted():
    """★★ 字段没匹配上 → 空值 + missing 计数，**不是**报错、更不是猜一个。

    ★ 为什么这条是核心：判据写错了是**人的错**，而人的错该被喊出来（missing 计数），
      不该交给 LLM 去"想办法"—— 它的办法通常是改用 LLM 中介的 extract，
      于是错误从"看得见的空"变成"看不见的错"。
    """
    # 这张卡里没有"热度"那一行（比如改版后它挪走了）
    result, _ = await _run(payload=_cards_payload([["测试商品乙", "¥8.8", "发布同款"]]))
    data = json.loads(result.extracted_content)
    assert data["rows"][0]["heat"] == ""
    assert data["missing"] == {"title": 0, "price": 0, "heat": 1}
    assert "heat 缺 1 张" in result.long_term_memory


@pytest.mark.asyncio
async def test_missing_reports_zero_for_fields_that_all_matched():
    """★ 全取到时也要有这一项（值 0），且摘要要明确说一句"都匹配上了"。

    一句话的**缺席**和一句话说"没问题"在产物里长得不一样，而人只会去看有字的地方。
    """
    result, _ = await _run(payload=_cards_payload([CARD]))
    data = json.loads(result.extracted_content)
    assert data["missing"] == {"title": 0, "price": 0, "heat": 0}
    assert "每个字段都匹配上了" in result.long_term_memory


# ── 三种失败形态 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_repeated_group_lists_what_was_found_instead():
    """★ 找不到组时，error 里必须带上"找到的组清单"。

    不含它，error 只能写"没找到列表" —— 而人在真站点上没法据此判断
    该把 group_index 改成几。这就是"抓错组"唯一的出路。
    """
    payload = {
        "found": False,
        "groupCount": 3,
        "inventory": [
            {"tag": "DIV", "memberCount": 6, "sample": "不限"},
            {"tag": "SPAN", "memberCount": 4, "sample": "手机App"},
        ],
    }
    result, _ = await _run(payload=payload)
    assert result.error
    assert "[0] 6 个 <DIV>" in result.error
    assert "[1] 4 个 <SPAN>" in result.error


@pytest.mark.asyncio
async def test_group_index_out_of_range_says_which_indices_exist():
    """`group_index` 指到不存在的组 —— 同上，要有清单可照着改。"""
    payload = _cards_payload([CARD])
    payload["found"] = False
    payload["groupCount"] = 1
    result, _ = await _run(payload=payload, group_index=3)
    assert result.error and "第 3 组" in result.error
    assert "[0] 1 个 <SPAN>" in result.error


@pytest.mark.asyncio
async def test_without_card_fields_it_says_why_instead_of_inventing_names():
    """★★ 本任务没配 card_fields → 明确说"缺判据"，而不是自己编字段名。

    ★ 对照实验：把这条实现成"那就返回空字段的行"的话，LLM 会看到一堆空值，
      然后自己发挥（编字段名 / 改用 extract 转录价格）。所以这条 error
      的措辞本身就是机制。
    """
    result, _ = await _run(fields=[], payload=_cards_payload([CARD]))
    assert result.error and "card_fields" in result.error
    assert "extract_table" in result.error, "要说清楚退路是哪个动作"


@pytest.mark.asyncio
async def test_no_page_is_an_error_not_a_crash():
    result = await M.extract_cards_impl(
        M.ExtractCardsAction(), _FakeSession(None), fields=FIELDS
    )
    assert result.error and M._NO_PAGE_MARKER in result.error


@pytest.mark.asyncio
async def test_unreadable_payload_is_an_error_not_a_guess():
    """返回值读不懂 → error。**不要**顺手改成"猜"（那会把读不到变成读到垃圾）。"""
    result, _ = await _run(raw="<html>不是 JSON</html>")
    assert result.error and "读不懂" in result.error


@pytest.mark.asyncio
async def test_evaluate_exception_is_reported_verbatim():
    result, _ = await _run(exc=RuntimeError("CDP 断了"))
    assert result.error and "CDP 断了" in result.error


# ── 截断与 payload 传递 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_truncated_is_reported_and_cards_are_trimmed():
    cards = [[f"商品{i}", "¥1.0", "热度 1", "发布同款"] for i in range(8)]
    payload = _cards_payload(cards[:3], truncated=True, total=8)
    result, _ = await _run(payload=payload, max_cards=3)
    data = json.loads(result.extracted_content)
    assert data["returned"] == 3 and data["total_cards"] == 8 and data["truncated"] is True
    assert "已截断" in result.long_term_memory


@pytest.mark.asyncio
async def test_params_reach_the_page_script():
    """参数真的传进了页内脚本 —— 漏传的话 group_index 会静默变成默认值 0。"""
    _, page = await _run(payload=_cards_payload([CARD]), group_index=2, min_members=5, max_cards=7)
    _, arg = page.calls[0]
    assert arg == {
        "groupIndex": 2,
        "minMembers": 5,
        "maxCards": 7,
        "maxMemberChars": M._MAX_MEMBER_CHARS,
    }


def test_script_skips_non_rendered_tags():
    """★★ 对照实验：SKIP 集合必须真的在脚本里，且**必须包含 SCRIPT**。

    ★ S8 探路的真实教训：第一版没滤脚本，于是"最大的重复块"是 **30 个行内
      埋点 <script>** —— 探测器忠实地回答了一个问错了对象的问题。

    ⚠️ 这条只证明**字符串在**（它是纯文本断言，不执行 JS）。它真的挡住了脚本
      这件事由 `tests/test_extract_cards_dom.py` 证 —— 那一页上 6 个 `<script>`
      比 4 张商品卡还多，去掉了 SKIP 就会当场读到埋点脚本。
      两条一起看才是完整的：一条钉配置，一条钉行为。
    """
    assert "SKIP" in M._CARDS_JS
    for tag in ("SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"):
        assert f"'{tag}'" in M._CARDS_JS, f"{tag} 必须在不渲染标签的黑名单里"


def test_script_sorts_groups_by_member_count():
    """组的顺序必须**按成员数降序**且稳定 —— group_index 的可复现性全靠它。"""
    assert "b.members.length - a.members.length" in M._CARDS_JS
    assert "sort" in M._CARDS_JS


# ── 启动门禁 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_round_trip_gate_passes_when_wired():
    await verify_extract_cards_round_trip(build_tools(card_fields=FIELDS), fields=FIELDS)


@pytest.mark.asyncio
async def test_round_trip_gate_skips_a_task_that_does_not_use_cards():
    """不用这个动作的任务不该因此跑不起来 —— 那是合法的。"""
    await verify_extract_cards_round_trip(build_tools(), fields=[])


@pytest.mark.asyncio
async def test_round_trip_gate_catches_a_broken_closure():
    """★★ 对照实验：**闭包没接上**必须被这条门禁抓住。

    ★ 这是这条门禁唯一真正独有的价值。tolls 里注册着动作、形状也全对，
      但 `build_tools(card_fields=...)` 那一环漏了 —— 于是 YAML 里三行判据
      一行都不会生效，而 LLM 会改用 extract（转录价格），一切看着正常。
      门禁靠"error 的措辞"把这种形态判出来（见 verify_extract_cards_round_trip）。
    """
    with pytest.raises(RuntimeError) as e:
        await verify_extract_cards_round_trip(build_tools(), fields=FIELDS)
    assert "闭包是空的" in str(e.value)


# ── DSL 侧：判据属于任务定义 ────────────────────────────────
def _spec(**over):
    base = {
        "schema_version": "1",
        "id": "t.cards",
        "start_url": "https://mocksite.local/cards",
        "goal": "读卡片列表",
        "steps": ["用 extract_cards 读取卡片列表。", "读完就结束。"],
        "output_model": "pdd.ProductRowList",
    }
    base.update(over)
    return load_task_str(json.dumps(base, ensure_ascii=False))


def test_card_fields_live_in_the_task_definition():
    spec = _spec(card_fields=[{"name": "price", "pattern": r"^\D*([\d.]+)"}])
    assert spec.card_fields[0].name == "price"


def test_a_broken_regex_is_rejected_at_load_time():
    """★ 正则编译不过必须在**加载期**报 —— 运行期报的话，报错会说"采集器执行失败"，
    而真正的错处是 YAML 里的一个括号。"""
    with pytest.raises(Exception) as e:
        _spec(card_fields=[{"name": "price", "pattern": "^¥([\\d.]+"}])
    assert "正则编译不过" in str(e.value)


def test_duplicate_field_names_are_rejected():
    with pytest.raises(Exception) as e:
        _spec(
            card_fields=[
                {"name": "price", "pattern": r"\d+"},
                {"name": "price", "pattern": r"¥(\d+)"},
            ]
        )
    assert "字段名重复" in str(e.value)


def test_declaring_fields_without_naming_the_action_is_rejected():
    """★★ 声明了判据却没有任何一步提到 extract_cards → 拒。

    ★ 挡的是一类静默失效：YAML 里郑重写了三行判据，而没有任何一步指示 LLM 用它。
      于是模型大概率改用库内置的 `extract`（LLM 中介），价格被转录一遍，
      而产物里一切正常 —— 那份判据就成了"躺在 YAML 里从没生效过"的死配置。
    """
    with pytest.raises(Exception) as e:
        _spec(card_fields=[{"name": "price", "pattern": r"\d+"}], steps=["读页面。", "结束。"])
    assert "extract_cards" in str(e.value)


def test_a_task_without_cards_is_unaffected():
    """绝大多数任务没有 card_fields —— 这个字段对它们必须完全无感。"""
    spec = _spec()
    assert spec.card_fields == []
