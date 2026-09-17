"""`extract_table` 的契约 —— 包括那个**最会误导人的报错**的对照实验。

★ 为什么这个文件必须存在（而不是"e2e 跑通就行了"）：
  e2e 用的是真浏览器，它只能证明"这次这个页面读对了"。
  而这里几条要钉死的是**注册期契约**：
    · `params` 会不会被当成给 LLM 的字段暴露出去（红线相邻）；
    · `browser_session` 的注入还成不成立；
    · `from __future__ import annotations` 会不会把注册搞坏。
  这三条在 e2e 里**要么看不出来，要么要等 40 秒浏览器启动之后才炸** ——
  而它们的报错都指不到原因，所以值得在毫秒级的单测里先钉死。

★ 这里**测不到**的部分要说清楚：`_TABLE_JS` 那段脚本的正确性
  （tHead/tBodies 的取法、innerText vs textContent、截断）在单测里是
  **测不了**的 —— 假 page 只会把我喂进去的 payload 原样吐回来。
  它只能由 e2e 用真 Chrome 打在真 HTML 上来验（tests/test_mock_pdd_e2e.py）。
  把这条写出来，是为了不让"单测全绿"被误读成"取表逻辑是对的"。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from browser_use import ActionResult, Tools

import ecom_agent.actions.extract_table as M
from ecom_agent.actions import build_tools, verify_extract_table_round_trip


# ── 桩：只回答 get_current_page()，不碰浏览器 ────────────────
class _FakePage:
    """记录 evaluate 收到了什么，并按预设回话。

    ★★★ `evaluate` **必须返回字符串**，这是这个桩最重要的一条，而它原先写错了。

      真实对象是 `browser_use.actor.page.Page`（不是 playwright 的 Page），
      它的 `evaluate(page_function, *args) -> str` **永远返回字符串**：
      对象走 `json.dumps`、`None` 变空串、数字布尔走 `str()`。

      而这里的桩原先 `return self.payload` —— 直接回一个 dict。于是
      "生产代码忘了 json.loads" 这件事在单测里**完全看不出来**：
      桩喂 dict、实现要 dict，两边一拍即合，13 条单测全绿。
      真浏览器里拿到的是 JSON 字符串，一跑就 `'str' object has no attribute 'get'`。

      ★ 教训不是"这个桩写错了"，而是：**桩的形状比真实对象的形状宽松时，
        它就不再是桩，而是一块遮羞布。** 桩应该模仿真实契约里最容易搞错的
        那一面 —— 包括"返回值类型反直觉"这种。所以这里显式 `json.dumps`，
        并且留了一个 `raw` 口子给"返回值不是合法 JSON"那一类对照实验。
    """

    def __init__(self, *, payload=None, exc=None, raw=None) -> None:
        self.payload = payload
        self.exc = exc
        self.raw = raw
        self.calls: list[tuple[str, object]] = []

    async def evaluate(self, script, arg):
        self.calls.append((script, arg))
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            # ★ 绕过 json.dumps：用来造"返回值不是合法 JSON / 是空串"的形态
            return self.raw
        return json.dumps(self.payload)


class _FakeSession:
    def __init__(self, page) -> None:
        self._page = page

    async def get_current_page(self):
        return self._page


def _table_payload(rows, *, headers=None, total=None, truncated=False):
    """造一份和 _TABLE_JS 返回结构一致的 payload。"""
    return {
        "found": True,
        "tableCount": 1,
        "headers": headers if headers is not None else ["商品ID", "标题", "价格"],
        "rows": rows,
        "totalRows": total if total is not None else len(rows),
        "truncated": truncated,
    }


# ── 注册期契约 ──────────────────────────────────────────────
def test_extract_table_is_registered_by_function_name():
    """动作名就是**函数名** —— 库用 `func.__name__`，装饰器上没有传名字的口子。

    ★ 这条钉的是"改名字"这个动作的后果：把 `register_extract_table` 里那个内层
      函数改名（比如为了"避免遮蔽模块级同名函数"），动作名就会跟着变，
      而 YAML 里 `match_action: [..., extract_table, ...]` 会**静默失效** ——
      规则照在、策略照加载、报告里照显示，就是一次都不命中。
    """
    tools = build_tools()
    assert M.EXTRACT_TABLE_ACTION in tools.registry.registry.actions
    assert M.EXTRACT_TABLE_ACTION == "extract_table"


def test_browser_session_is_not_exposed_to_the_llm():
    """★ 红线相邻：`browser_session` 是保留参数名，**不能**出现在给 LLM 的 schema 里。

    库会把 `SpecialActionParameters` 的 9 个字段名从 tool schema 里整个剥掉
    （`registry/service.py:278`），所以它不该出现。它出现了就意味着
    "库注入"变成了"让模型填一个 BrowserSession 对象" —— 模型填不出来，
    于是这个动作永远调不动。

    反过来，也确认 `params` **没有**被当成一个字段暴露出去：
    那正是"忘了给 param_model="的症状（Type 2 会把 params 本身当字段）。
    """
    props = M.ExtractTableAction.model_json_schema()["properties"]
    assert set(props) == {"table_index", "max_rows"}, (
        f"给 LLM 的参数集合变了：{sorted(props)} —— "
        "多了 browser_session 说明特殊参数没被剥掉；多了 params 说明 param_model 没给对"
    )


def test_control_experiment_future_annotations_break_action_registration():
    """★★★ 对照实验：**同一个 action，只改一个 import**，一个能注册、一个注册不了。

    ⚠️ 这是本项目见过的最会误导人的报错。实测原文：

        ValueError: Action 'extract_table' parameter 'browser_session: BrowserSession'
                    conflicts with special argument injected by tools:
                    'browser_session: BrowserSession'

      两边的名字和类型**逐字相同**。第一反应必然是"库有 bug 吧"，
      而真相是：PEP 563 把注解变成了**字符串**，库拿到的是 `'BrowserSession'`
      这个 str，于是 `param_type == expected_type` 恒为 False
      （`registry/service.py:130-155` 做的是运行时 `==` 比较，不是类型推导）。

    ★ 为什么这个坑值得一条专门的测试：
      它**取决于 action 有没有特殊参数**，不取决于有没有写那行 import。
      同目录的 `guard_gate.py` 里那个 action 只收一个 message，
      所以它带着 future import 也能跑 —— 于是"照抄隔壁那个能跑的文件"
      就成了一个会踩的路径。实测与结论记在 `docs/spikes.md`。
    """
    body = """
{header}
from browser_use import ActionResult, BrowserSession, Tools


def build():
    t = Tools()

    @t.action("探针")
    async def probe(params: int, browser_session: BrowserSession) -> ActionResult:
        return ActionResult()

    return t
"""
    # ★★ `dont_inherit=True` 不是可选项 —— 少了它这个对照实验会**两个都失败**：
    #   `compile()` 默认 `dont_inherit=False`，于是编译单元会
    #   【继承调用方代码里的 future 标志】。本测试文件顶部自己就写了
    #   `from __future__ import annotations`，于是"不写 future"的那一段
    #   也被字符串化了 —— 对照实验的对照组当场消失，
    #   失败的还是那句一模一样的报错，看起来像"这个坑到处都在"。
    #   实测踩到过。**一个自己会骗人的对照实验比没有对照实验更坏。**
    def _compile(header: str, name: str):
        return compile(body.format(header=header), name, "exec", dont_inherit=True)

    # 不写 future import → 注册成功
    ns_ok: dict = {}
    exec(_compile("", "<no-future>"), ns_ok)
    ns_ok["build"]()

    # 写上 → 注册失败，而且报错是那句"两边一模一样"的
    ns_bad: dict = {}
    exec(_compile("from __future__ import annotations", "<future>"), ns_bad)
    with pytest.raises(ValueError) as ei:
        ns_bad["build"]()
    msg = str(ei.value)
    assert "conflicts with special argument injected by tools" in msg
    # ★ 报错里两边确实长得一样 —— 这正是它误导人的地方，断言下来免得日后被"美化"掉
    assert "browser_session: BrowserSession" in msg


def test_extract_table_module_does_not_use_future_annotations():
    """★ 上一条测的是"这个坑存在"，这条测的是"我们没踩"。

    ★ 用 ast 而不是 `"from __future__ import annotations" in 源码文本`：
      本模块的 docstring 里**逐字写着**这行 import（在解释它为什么不能写），
      朴素子串匹配会把那段说明误判成真的写了 —— 一个由注释触发的假警报。
      ast 只看真正的语法节点，注释和字符串都不算。
    """
    src = Path(M.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
    ]
    assert not offenders, (
        "extract_table.py 里出现了 `from __future__ import annotations` —— "
        "它会让动作注册直接抛 'conflicts with special argument injected by tools'，"
        "而那条报错的两边长得一模一样。见本文件顶部的对照实验。"
    )


# ── 实现体的行为（用桩 session/page）────────────────────────
@pytest.mark.asyncio
async def test_returns_error_when_there_is_no_page():
    """没有页面时返回 error，而不是抛异常、也不是返回空表。

    ★ 差别很重要：抛异常会被 `Tools.act` 的 `except Exception` 吞成
      ActionResult(error=...)，结果"看起来一样"但理由丢了。
      而返回 `rows: []` 更糟 —— 那会被上游当成"这个页面确实没有商品"，
      变成一次**合法的空结果**（parse_status=empty），没人会去查。
    """
    r = await M.extract_table_impl(M.ExtractTableAction(), _FakeSession(None))
    assert isinstance(r, ActionResult)
    assert r.error and "没有可用页面" in r.error
    assert r.extracted_content is None


@pytest.mark.asyncio
async def test_happy_path_returns_json_and_a_short_memory():
    rows = [["100001", "保温杯 316不锈钢", "¥59.90"], ["100002", "保温杯 便携款", "¥39.00"]]
    page = _FakePage(payload=_table_payload(rows))
    r = await M.extract_table_impl(M.ExtractTableAction(), _FakeSession(page))

    assert r.error is None
    data = json.loads(r.extracted_content)
    assert data["rows"] == rows
    assert data["headers"] == ["商品ID", "标题", "价格"]
    assert data["total_rows"] == 2 and data["truncated"] is False

    # ★ long_term_memory 必须是**摘要**，不能是整份 JSON：
    #   它会进 LLM 后续每一步的上下文。这里用"不含行数据"来钉死这一点，
    #   而不是断言一个具体的字符串（那种断言改个措辞就红，没有信息量）。
    assert r.long_term_memory and "2 行" in r.long_term_memory
    assert "100001" not in r.long_term_memory

    # ★ 中文不被转义成 \uXXXX —— 审计日志是给人看的
    assert "保温杯" in r.extracted_content


@pytest.mark.asyncio
async def test_params_reach_the_js_unchanged():
    """`table_index` / `max_rows` 要原样传给页面脚本。"""
    page = _FakePage(payload=_table_payload([]))
    await M.extract_table_impl(
        M.ExtractTableAction(table_index=2, max_rows=7), _FakeSession(page)
    )
    _script, arg = page.calls[0]
    assert arg == {"tableIndex": 2, "maxRows": 7}


@pytest.mark.asyncio
async def test_truncation_is_reported_not_hidden():
    """截断必须**说出来**。静默丢弃行 = 报告里少了几件商品而没人知道。"""
    page = _FakePage(payload=_table_payload([["1", "a", "1.00"]], total=99, truncated=True))
    r = await M.extract_table_impl(
        M.ExtractTableAction(max_rows=1), _FakeSession(page)
    )
    data = json.loads(r.extracted_content)
    assert data["truncated"] is True and data["total_rows"] == 99
    assert "已截断" in r.long_term_memory


@pytest.mark.asyncio
async def test_missing_table_reports_how_many_were_found():
    """★ 报错里要带"共找到 N 个表格"。

      只报"没找到第 2 个表格"的话，看的人不知道是自己数错了、
      还是页面上一个表格都没有 —— 而这两种情况的修法完全不同
      （改 table_index vs 页面根本没加载完）。
    """
    page = _FakePage(payload={"found": False, "tableCount": 1})
    r = await M.extract_table_impl(
        M.ExtractTableAction(table_index=2), _FakeSession(page)
    )
    assert r.error and "共找到 1 个" in r.error


@pytest.mark.asyncio
async def test_js_exception_becomes_an_action_result_not_a_raise():
    """页面脚本抛异常时，变成 ActionResult(error=...) 而不是往上抛。

      理由同"没有页面"那条：抛出去也会被库吞成 error，但理由会丢。
      这里额外断言**异常信息被带出来了** —— 否则 LLM 只看到"失败了"，
      而它需要"因为什么失败"才能改道。
    """
    page = _FakePage(exc=RuntimeError("Execution context was destroyed"))
    r = await M.extract_table_impl(M.ExtractTableAction(), _FakeSession(page))
    assert r.error and "Execution context was destroyed" in r.error


def test_params_reject_unknown_fields():
    """`extra="forbid"`：模型多编一个字段就报错触发重试，而不是静默忽略。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        M.ExtractTableAction(table_index=0, nonsense="x")


# ── 返回值解析：那个 evaluate 给的是【JSON 字符串】 ──────────
@pytest.mark.asyncio
async def test_evaluate_result_is_json_text_and_must_be_parsed():
    """★★★ 对照实验的主角：`page.evaluate` 给的是**字符串**，不是 dict。

    真实契约（`browser_use.actor.page.Page.evaluate`，实测）：
    `evaluate(page_function, *args) -> str`，docstring 原文
    "Objects and arrays are JSON-stringified"。

    ★ 这条断言的是**桩本身忠实**：让 `_FakePage` 回一份 payload，
      手动确认拿到的确实是一段字符串。
      —— 它守的是"上面那个桩不要再被改回直接回 dict"。
      桩一旦回 dict，"实现忘了解析"就会重新变得不可见
      （这正是修复前 13 条全绿的成因）。
    """
    page = _FakePage(payload=_table_payload([["100001", "保温杯", "¥59.90"]]))
    raw = await page.evaluate(M._TABLE_JS, {"tableIndex": 0, "maxRows": 100})
    assert isinstance(raw, str), (
        "桩又回 dict 了 —— 真实对象的 evaluate 永远回字符串。"
        "把它改回 dict 会让『实现忘记 json.loads』在单测里重新变成不可见的。"
    )
    assert json.loads(raw)["rows"] == [["100001", "保温杯", "¥59.90"]]


def test_decode_rejects_every_shape_that_is_not_a_table_object():
    """解析器的三种坏形态：空串 / 非 JSON 裸串 / JSON 但不是对象。

    ★ 三种都必须是 None（→ 调用方转成一条 error），**不能**是"猜一个默认值"：
      猜错会让"读不到表格"变成"读到了垃圾"，而后者看起来像成功。

    ★ 顺带钉住"数字/布尔会被 str() 化"这条：真实契约里
      `evaluate` 对非对象返回值走 `str(value)`，所以 `"42"` 是可能出现的，
      而它 `json.loads` 之后是个 int，不是 dict —— 必须被拒。
    """
    assert M._decode_table_payload("") is None            # JS 返回 null/undefined
    assert M._decode_table_payload("   ") is None         # 只有空白
    assert M._decode_table_payload("不是 JSON") is None    # 裸串
    assert M._decode_table_payload('{"a": ') is None      # JSON 坏了
    assert M._decode_table_payload("42") is None          # 数字 -> int
    assert M._decode_table_payload('["x"]') is None       # 数组
    assert M._decode_table_payload(None) is None          # 压根不是 str
    assert M._decode_table_payload({"found": True}) is None  # ★ dict 也要拒
    assert M._decode_table_payload('{"found": false}') == {"found": False}


@pytest.mark.asyncio
async def test_unreadable_evaluate_result_is_an_error_not_a_crash():
    """读不懂时给 LLM 一条 error，而不是抛 —— 并且**把原文带出来**。

    ★ 为什么要把前 200 字符带出来：这条错误唯一有价值的线索就是"它到底
      给了什么"。只写"返回值读不懂"的话，排查要从"重新起一个浏览器、
      把页面 dump 下来"开始。
    """
    page = _FakePage(raw="<html>不是 JSON</html>")
    r = await M.extract_table_impl(M.ExtractTableAction(), _FakeSession(page))
    assert r.error and "读不懂" in r.error
    assert "不是 JSON" in r.error


@pytest.mark.asyncio
async def test_a_dict_payload_is_now_rejected_which_proves_parsing_is_load_bearing():
    """★ 反向对照：把"旧桩的行为"（直接给 dict）喂进来，必须**失败**。

    这条是上面那个修复的**可证伪锚点**：
      · 如果有人把实现里的 `_decode_table_payload(raw)` 改回
        `raw`（即不再解析），这条会红；
      · 而它红了恰恰说明"解析"这一步是**承重的**，不是装饰。

    没有这一条的话，"我加了 json.loads"和"我没加"在测试里长得一样 ——
    因为其他用例都从桩那儿拿到了合法的 JSON 字符串。
    """
    page = _FakePage(raw={"found": True, "tableCount": 1, "headers": [],
                          "rows": [], "totalRows": 0, "truncated": False})
    r = await M.extract_table_impl(M.ExtractTableAction(), _FakeSession(page))
    assert r.error, "dict 直接喂进来竟然成功了 —— 说明解析那一步被绕过去了"


# ── 启动自检本身 ────────────────────────────────────────────
# ★★ 这两条为什么是 `async def`（pytest.ini 有 asyncio_mode=auto）：
#
#   它们验的是"这个门禁能不能在生产路径上跑起来"，而生产路径
#   （`runner.TaskRunner.run()`）是**协程**。原来的写法是同步用例
#   调一个内部 `asyncio.run()` 的同步函数 —— 于是这里全绿，
#   而真 run 一启动就 `RuntimeError: asyncio.run() cannot be called
#   from a running event loop`。
#
#   也就是说：**用例的颜色和被测路径的颜色不一致，本身就是测不到。**
#   改 async 之后，如果哪天有人再把门禁改回同步实现，这两条会立刻红 ——
#   它们顺手变成了"颜色契约"的哨兵。
async def test_verify_round_trip_passes_for_the_real_registry():
    await verify_extract_table_round_trip(build_tools())


async def test_verify_round_trip_fails_loudly_when_not_registered():
    """对照：没注册时必须**当场抛**，而不是安静地什么都不做。

    ★ 没有这一半的话，一个"永远直接 return"的实现也能让上一条通过 ——
      而那种自检等于没有，它只会让人以为查过了。
    """
    with pytest.raises(RuntimeError) as ei:
        await verify_extract_table_round_trip(Tools())
    assert M.EXTRACT_TABLE_ACTION in str(ei.value)
