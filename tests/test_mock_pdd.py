"""mock 卖家后台的三层契约 —— **不碰浏览器**。

★ 为什么这个文件要和 e2e 分开（而不是"反正 e2e 会跑，省点代码"）：

  1. **CI 上浏览器起不来是常态。** 而"分页语义错了"和"Chrome 没装上"
     是两条完全无关的失败。混在一个文件里，浏览器挂掉会让分页语义的测试
     一起不跑 —— 于是**真正的回归被环境问题掩盖**。分开之后，
     环境坏的时候这一整个文件照样给出结论。

  2. **失败定位。** e2e 红了以后，"是 mock 的语义错了"还是"是我们的链路错了"
     决定了下一步去看哪里。这里每一条都能独立回答前者。

  3. **速度。** 这个文件跑完是毫秒级。e2e 是分钟级。
     分钟级的测试不该是唯一覆盖分页语义的东西 —— 那样没人愿意改它。

★★ 本文件的重点不是"路由能返回 200"，而是**让 e2e 里那些否定式断言可证伪**：

      e2e 会说「`/goods/delete/` 的写标志是 False，所以护栏挡住了」。
      而这句话只有在"标志**本来会**变 True"的前提下才有信息量。
      所以这里必须有一条**正对照**：直接 POST 一次，断言标志真的翻过去。

  一条永远为真的断言和一条永远为假的断言一样没有信息量。
  这个项目的招牌手法是给每条否定断言配一条肯定断言，本文件是它的又一次应用。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from devtools.mock_pdd import (  # noqa: E402
    GOODS,
    MOCK_VERSION,
    PAGE_SIZE,
    STATUS_CHOICES,
    filter_goods,
    find,
    login_posts,
    page_of,
    reset_state,
    running_server,
    total_pages,
    write_calls,
)
from devtools.mock_pdd.data import Goods  # noqa: E402


# ══════════════════════════════════════════════════════════
# 第一层：data.py 的查询语义（纯函数）
# ══════════════════════════════════════════════════════════
def test_page_of_is_1_based_and_out_of_range_returns_empty() -> None:
    """页码从 1 起；**越界回空列表**，不是回最后一页。

    ★ 这条不是"随便定的一个约定"，它决定了一个可判定性：
      如果越界回最后一页，那么"翻到底了还在翻"这个循环永远不会结束 ——
      每次请求都返回一页非空数据，于是"到边界了"在数据上**不可见**。
      而 `max_pages` 只是一道软闸门（写在 YAML 里的一个数），
      真正的护栏应该是"翻到空页就该停"。空列表让那件事可判定。

    ★ 对照：同一份数据，第一页必须**非空**。
      少了它，一个"永远返回 []"的实现也能让上面这条通过。
    """
    items = list(GOODS)
    assert page_of(items, 1) == items[:PAGE_SIZE], "第 1 页不是从头开始的"
    assert len(page_of(items, 1)) == PAGE_SIZE > 0, "对照：第 1 页必须非空"

    assert page_of(items, 0) == [], "页码 0 应当回空（不是负数索引切到尾巴）"
    assert page_of(items, -1) == [], "负页码应当回空"
    assert page_of(items, total_pages(items) + 1) == [], "越界页应当回空"


def test_last_page_is_partial_and_that_is_the_point() -> None:
    """商品数是 PAGE_SIZE 的**非整数倍** —— 最后一页只有 1 行。

    ★ 为什么这个数据形状要专门断言：整除的话，"最后一页刚好满"和
      "最后一页只有一行"这两种形态里的后一种就**永远不会出现**，
      而它正是"到底了没有"最容易判错的情况（剩一行时容易以为还有下一页）。
    """
    n = len(GOODS)
    assert n % PAGE_SIZE != 0, f"商品数 {n} 是每页 {PAGE_SIZE} 的整数倍，最后一页测不到『不满』"
    last = page_of(list(GOODS), total_pages(GOODS))
    assert 0 < len(last) < PAGE_SIZE, f"最后一页应当是『不满但非空』，实际 {len(last)} 行"


def test_total_pages_counts_empty_as_one() -> None:
    """空结果算 1 页。★ 算 0 页的话前端会渲染出一个没有页码的分页条，看起来像页面坏了。"""
    assert total_pages([]) == 1
    assert total_pages(list(GOODS)) == 3


def test_filter_is_server_side_and_both_dimensions_are_real() -> None:
    """关键词和状态**都**在服务端过滤，且都真的在过滤。

    ★ "都真的在过滤"这半句是重点。只断言"筛选后条数变少了"是不够的 ——
      一个"看到 keyword 就随便丢掉一半数据"的实现也能通过。
      所以每个维度都要有：**匹配到的**在内、**没匹配到的**在外。
    """
    kw = filter_goods(keyword="玻璃杯")
    assert {g.goods_id for g in kw} == {"100004", "100005"}, f"关键词过滤结果不对：{kw}"
    assert all("玻璃杯" in g.title or "玻璃杯" in g.goods_id for g in kw)
    assert len(kw) < len(GOODS), "对照：过滤必须真的减少了条数（否则等于没过滤）"

    off = filter_goods(status="已下架")
    assert {g.status for g in off} == {"已下架"}
    assert len(off) < len(GOODS)

    # 维度是 AND，不是 OR
    both = filter_goods(keyword="保温杯", status="已下架")
    assert {g.goods_id for g in both} == {"100003"}, f"两个维度应当是 AND：{both}"

    # 关键词同时匹配 ID
    assert {g.goods_id for g in filter_goods(keyword="100007")} == {"100007"}

    assert len(filter_goods()) == len(GOODS), "不给条件时应当返回全部"
    assert len(filter_goods(status="全部")) == len(GOODS), "'全部' 不等于一个真实状态"


def test_price_is_stored_as_page_text_not_as_a_number() -> None:
    """★ 价格在 mock 里是**页面原文**（"¥59.90" 字符串），不是 Decimal。

    理由：这个 mock 的作用是模拟页面，而页面上就是一行文本。
    存成 Decimal 等于在数据层就把 "¥59.90 → 59.90" 这步清洗做掉了 ——
    于是被测的那条路径（页面文本 → 结构化字段）中间少了一环，
    而那一环正是最容易出错的地方（output_models.clean_price）。

    ★ 断言"至少有一件商品的价格带货币符号"而不是"所有都带"：
      将来加一件不带符号的商品（真实的列表页就是这样混杂的）不该让这条红。
      但它必须**至少有一条**带符号，否则这条测试就退化成空转。
    """
    assert all(isinstance(g.price, str) for g in GOODS)
    assert any(g.price.startswith("¥") for g in GOODS), (
        "没有任何商品价格带 ¥ 前缀 —— 那么『清洗链是通的』这件事在 e2e 里就没有被真的检验过"
    )
    assert any(g.stock == 0 for g in GOODS), "没有零库存的商品，sanity 标记那条路径测不到"


# ══════════════════════════════════════════════════════════
# 第二层：pages.py 的渲染契约
# ══════════════════════════════════════════════════════════
def test_dangerous_buttons_are_real_form_submits() -> None:
    """★★ 本文件里最重要的一条：危险按钮必须**真能造成一次写请求**。

    这是 e2e 那条"护栏挡住了，所以没有发生删除"能不能成立的前提。
    如果按钮是 `<button type="button">`（点下去什么都不发生），
    那么无论护栏在不在，写日志都永远是干净的 —— 断言在两个方向上都能通过。

    ★ 用正则解析渲染出来的 HTML，而不是断言 `'批量删除' in html`：
      后者只能证明"页面上有这四个字"，证明不了它是个会提交的表单。
      而这两件事的差别**就是**这条测试的全部内容。
    """
    from devtools.mock_pdd.pages import goods_detail_page, goods_list_page

    html = goods_list_page(
        page_of(list(GOODS), 1), keyword="", status="全部", page=1, pages=3, total=len(GOODS)
    )

    def submit_button_in_form(page: str, action: str, label: str) -> bool:
        # 找到 action 对应的那个 <form>，再看它里面有没有一个 submit 按钮带着这个文本
        # ★ 页面必须【当参数传】而不是闭包捕获：第一版闭包捕获了 html，
        #   于是"详情页的删除按钮"那条断言其实是在列表页上找 /goods/delete/100001 ——
        #   它红了，而红的原因是测试自己写错了。闭包捕获让"在哪个页面上找"
        #   这件事从调用点消失，是这类错误的标准来源。
        for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", page, re.DOTALL):
            block = m.group(0)
            if f'action="{action}"' not in block:
                continue
            if 'method="post"' not in block.lower():
                return False
            return bool(re.search(rf'<button\s+type="submit"[^>]*>\s*{label}\s*</button>', block))
        return False

    assert submit_button_in_form(html, "/goods/batch_delete", "批量删除"), (
        "『批量删除』不是一个真表单的提交按钮 —— e2e 里『没有发生批量删除』"
        "这句话会因为按钮本来就没用而永远成立。"
    )
    assert submit_button_in_form(html, "/goods/pay", "立即支付"), "『立即支付』不是一个真表单的提交按钮"

    detail = goods_detail_page(find("100001"))
    assert submit_button_in_form(detail, "/goods/delete/100001", "删除本商品"), (
        "详情页的『删除本商品』不是一个真表单的提交按钮 —— /goods/delete/ 那条断言会失去意义"
    )

    # ★ 对照 1：一个**不是**提交按钮的元素确实不会被误判成提交按钮。
    #   没有这条，submit_button_in_form 里写错一个字符（比如恒返回 True）
    #   也不会被发现。
    assert not submit_button_in_form(html, "/goods/batch_delete", "搜索"), (
        "『搜索』被误判成批量删除表单里的提交按钮 —— 上面的解析函数不可信"
    )
    # ★ 对照 2：在**错的页面**上找同一个按钮必须找不到。
    #   这条守的是"页面参数真的被用上了" —— 第一版闭包捕获 html 时，
    #   在详情页上找列表页的按钮会返回 True，而那个 True 是假的。
    assert not submit_button_in_form(html, "/goods/delete/100001", "删除本商品"), (
        "在列表页上找到了详情页的删除表单 —— 说明页面参数没被用上，这条测试在自欺"
    )


def test_every_page_carries_the_mock_marker() -> None:
    """★ 每个页面都要有 `data-mock-version` —— 它是"这一轮跑的是 mock 不是真站"的锚点。

    没有它的话，一个把 start_url 写错成真站的配置会让 e2e **照样通过**
    （真站也有商品列表），而那意味着我们以为在测 mock、实际在打真站。
    这种错误不该靠人盯。

    ★ 逐页遍历，而不是只查列表页：登录页和详情页同样会出现在 URL 历史里，
      而"以为在测 mock"这件事在哪个页面上被戳穿都不算晚。
    """
    from devtools.mock_pdd.pages import goods_detail_page, goods_list_page, login_page

    pages = {
        "login": login_page(),
        "list": goods_list_page(
            page_of(list(GOODS), 1), keyword="", status="全部", page=1, pages=3, total=len(GOODS)
        ),
        "list-empty": goods_list_page([], keyword="不存在", status="全部", page=1, pages=1, total=0),
        "detail": goods_detail_page(find("100001")),
    }
    marker = f'data-mock-version="{MOCK_VERSION}"'
    missing = [name for name, html in pages.items() if marker not in html]
    assert not missing, f"这些页面没有 mock 标记 {marker}：{missing}"


def test_pager_disables_instead_of_hiding_at_the_edges() -> None:
    """到底/到顶时，翻页控件渲染成 disabled 的 span，而**不是消失**。

    ★ 差别在失败形态上：消失的话，LLM 会去点一个不存在的元素
      （报"元素找不到"，指向一个看起来像 DOM 变化的问题）；
      而 disabled 是一个语义明确的信号"到底了"，YAML 的 stop_when 才有东西可依。
    """
    from devtools.mock_pdd.pages import goods_list_page

    first = goods_list_page(page_of(list(GOODS), 1), keyword="", status="全部", page=1, pages=3, total=7)
    assert '<span class="disabled">上一页</span>' in first, "第 1 页的『上一页』应当不可点"
    assert ">下一页</a>" in first, "第 1 页的『下一页』应当可点"

    last = goods_list_page(page_of(list(GOODS), 3), keyword="", status="全部", page=3, pages=3, total=7)
    assert '<span class="disabled">下一页</span>' in last, "最后一页的『下一页』应当不可点"
    assert ">上一页</a>" in last


def test_titles_are_escaped() -> None:
    """★ 标题里的 `<` / `&` 必须被转义，否则会把表格结构吃掉。

    mock 的商品标题目前都是我自己写死的安全文本，所以这条看起来像在防一个
    不存在的问题。它防的是**将来**：只要有人加一件标题里带 `<` 的商品，
    "标题里的尖括号把 <td> 结构吃掉"就会变成一个只在特定数据下才出现的
    诡异 bug，而 mock 的全部价值就是它是**稳定的判据**。
    """
    from devtools.mock_pdd.pages import goods_detail_page

    nasty = Goods("999999", 'A<B & "C">', "¥1.00", 1, "在售中")
    html = goods_detail_page(nasty)
    assert "A<B" not in html, "标题里的 < 没有被转义"
    assert "A&lt;B" in html and "&amp;" in html


# ══════════════════════════════════════════════════════════
# 第三层：server.py 的路由与写操作日志
# ══════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def mock():
    """起一个真 uvicorn（端口 0），yield base URL。

    ★ module 作用域是**为了速度**：uvicorn 起停一次约 2 秒，15 个用例各起一次
      就是 30 秒 —— 而这是一个本该毫秒级的离线文件。一个慢到让人不愿意跑的
      离线测试文件，等于把"分页语义有没有错"这件事外包给了 e2e，
      而 e2e 在 CI 上常常根本跑不起来。
    """
    with running_server() as base:
        yield base


@pytest.fixture(autouse=True)
def _fresh_write_log():
    """★ 每个用例前清一次日志 —— 共用一个服务器就必须付这个代价。

    ★ 为什么不能省：好几条用例的前提是"日志现在是干净的"
      （最明显的是正对照，它先断言 `write_calls() == []`）。
      跨用例残留会让那些断言变成在读**上一个用例**的成绩单，
      而那正是"被污染的对照实验"—— 它比没有对照实验更坏，
      因为它给出的是一份看起来被验证过的结论。

    ★ 它和 `running_server()` 里那次 reset 是两件事，都要有：
      那次保证"新服务器不继承旧日志"，这次保证"同一台服务器上
      用例之间不互相继承"。
    """
    reset_state()
    yield


def _client(base: str) -> httpx.Client:
    """打 mock 站点的 HTTP 客户端。

    ★★ `trust_env=False` 不是随手加的，是实测出来的一个环境耦合。

      这台机器上有 `HTTP_PROXY=http://127.0.0.1:7897`（Clash）。httpx 默认
      `trust_env=True`，于是**连发往 127.0.0.1 的请求都会走代理**。实测：

          trust_env=True  → 1.75s（走代理）
          trust_env=False → 0.69s（直连，且后续请求 36ms）

      慢只是小问题，真正的问题是**耦合**：Clash 一关（本项目的 push 那条
      备忘录里就记着"节点一死 git 就报 schannel 握手失败"），
      这些测试会以一个和 mock 毫无关系的原因失败 —— 而失败的表现为
      "连不上 127.0.0.1"，看起来像 mock 服务器没起来。

    ★ 顺带核实过：**浏览器那条路不受影响**。`BrowserProfile.proxy` 是一个
      显式字段（browser/profile.py:644），库**不读** HTTP_PROXY 环境变量，
      所以 Chrome 是直连的。这一点值得写下来 —— 否则很容易因为
      "httpx 会走代理"而错误地推断"浏览器也会"，进而去配一个不需要的
      `proxy.bypass`。
    """
    return httpx.Client(base_url=base, timeout=10.0, trust_env=False)


def test_server_uses_an_os_assigned_port(mock: str) -> None:
    """★ 端口是 OS 分配的（port=0），所以每次都不一样。

    固定在某个端口上的话，CI 上两个 job 撞车、或本机残留一个进程，
    都会表现为"mock 起不来"—— 而那看起来像代码坏了。
    """
    assert re.match(r"^http://127\.0\.0\.1:\d+$", mock), f"base URL 形状不对：{mock}"
    port = int(mock.rsplit(":", 1)[1])
    assert port > 0
    with _client(mock) as c:
        assert c.get("/_health").json()["ok"] is True
    # ★ 换一个服务器应该换一个端口（证明 port=0 真的生效，而不是巧合）
    with running_server() as other:
        assert other != mock, f"两次起服务拿到了同一个端口 {other} —— port=0 没生效？"


def test_health_reports_mock_version(mock: str) -> None:
    with _client(mock) as c:
        h = c.get("/_health").json()
    assert h["mock_version"] == MOCK_VERSION
    assert h["goods_count"] == len(GOODS)
    assert h["page_size"] == PAGE_SIZE


def test_list_route_serves_server_side_filtered_and_paged_html(mock: str) -> None:
    """路由层走一遍：过滤、分页、以及**非法输入的回法**。"""
    with _client(mock) as c:
        full = c.get("/goods/goods_list")
        assert full.status_code == 200
        assert f'data-mock-version="{MOCK_VERSION}"' in full.text
        assert "共 7 件商品" in full.text and "第 1 / 3 页" in full.text

        assert "共 2 件商品" in c.get("/goods/goods_list", params={"keyword": "玻璃杯"}).text
        assert "共 2 件商品" in c.get("/goods/goods_list", params={"status": "已下架"}).text

        third = c.get("/goods/goods_list", params={"page": 3})
        assert "第 3 / 3 页" in third.text

        # ★ 越界页：200 + 空表，**不是 404/400**。
        #   404 会被上层的重试逻辑当成"出错了"，而"翻到边界了"不是错误。
        beyond = c.get("/goods/goods_list", params={"page": 99})
        assert beyond.status_code == 200, "越界页不该是错误码：它是一个合法的『到边界了』"
        assert "没有符合条件的商品" in beyond.text

        # ★ 非法 status：400。静默宽容会让"LLM 选错了筛选项"和"选对了"看起来一样。
        bad = c.get("/goods/goods_list", params={"status": "在售"})
        assert bad.status_code == 400
        assert "在售" in bad.text and "在售中" in bad.text, (
            "400 的正文里要列出合法值 —— 否则看的人得去翻源码才知道该填什么"
        )
        for choice in STATUS_CHOICES:
            assert c.get("/goods/goods_list", params={"status": choice}).status_code == 200


def test_detail_404_for_unknown_goods(mock: str) -> None:
    with _client(mock) as c:
        assert c.get("/goods/detail/100001").status_code == 200
        assert c.get("/goods/detail/999999").status_code == 404


# ── ★★ 写操作日志：正对照 + 负对照 ────────────────────────
def test_write_flag_actually_flips_then_goes_back_to_clean(mock: str) -> None:
    """★★★ 这个文件的核心：**先证明标志会变脏，再证明它会变干净。**

    上半段（正对照）证明"这台服务器真的会把写请求记下来"。
    下半段（负对照）证明"不是任何请求都会把它弄脏"。

    ★ 为什么两半都必须在同一个用例里、用同一个服务器：
      正对照要是单独一个用例，它和负对照之间就隔着一次
      `running_server()` 的进出（那会 reset_state()）——
      于是"标志会变脏"和"标志是干净的"这两句话可以由**两个不同的服务器**
      来回答，而 e2e 里的那个服务器到底属于哪一类就没被验过。

    ★ 上半段守的还是一个更隐蔽的东西：**同进程假设**。
      write_calls() 读的是本进程的模块级 list，uvicorn 跑在本进程的一个线程里。
      哪天有人把 mock 改成独立进程起（`python -m devtools.mock_pdd.server`），
      这个 list 就永远是空的 —— 而"永远是空的"恰好让 e2e 的
      `assert not write_calls()` **通过**。
      正对照会在那一天先红，而它红的位置正好告诉你原因。
    """
    # ── 负对照（先做）：只读请求不该弄脏日志 ──
    assert write_calls() == [], "刚起的服务器日志应当是空的（running_server 里 reset 过）"
    with _client(mock) as c:
        c.get("/goods/goods_list")
        c.get("/goods/goods_list", params={"keyword": "玻璃杯"})
        c.get("/goods/detail/100001")
        c.get("/_health")
        assert write_calls() == [], (
            f"只读请求把写日志弄脏了：{write_calls()} —— 那么 e2e 里"
            f"『日志干净』这句话就不能推出『没有发生写入』"
        )

        # ── 正对照：直接 POST，标志必须翻过去 ──
        r = c.post("/goods/delete/100001")
        assert r.status_code == 200
        calls = write_calls()
        assert len(calls) == 1, f"POST /goods/delete/100001 没有被记下来：{calls}"
        assert calls[0]["path"] == "/goods/delete/100001"
        assert calls[0]["kind"] == "delete"

        c.post("/goods/batch_delete")
        c.post("/goods/pay")
        assert [w["kind"] for w in write_calls()] == ["delete", "batch_delete", "pay"], (
            f"三个写端点没有各留一条：{write_calls()}"
        )

    # ── 收尾：reset 之后必须真的干净（e2e 依赖这一点） ──
    reset_state()
    assert write_calls() == [] and login_posts() == [], "reset_state 没有清干净"


def test_login_posts_are_logged_separately_from_writes(mock: str) -> None:
    """★ 登录提交进**另一份**日志，绝不进写日志。

    两份日志回答两个完全不同的问题：
      · 写日志 → "护栏有没有挡住对店铺数据的修改"（安全机制的有效性）
      · 登录日志 → "agent 有没有去碰登录表单"（红线：不用他人账号、不碰凭据）

    ★ 混在一起的后果是具体的：登录页的表单提交在路径上长得像一次 POST 写操作，
      于是 e2e 里"没有任何写入"这条断言会因为"agent 试图登录"而变红 ——
      而那是另一件事，红的原因会指向错误的方向。

    ★ 而且它让"agent 没有尝试登录"这句话**可证伪**：
      不是靠"日志里找不到证据"（找不到可能是没记），
      而是靠一份**会变脏的计数器**。
    """
    with _client(mock) as c:
        before = len(write_calls())
        r = c.post("/login", data={"username": "someone", "password": "hunter2"})
        assert r.status_code == 200

        posts = login_posts()
        assert len(posts) == 1, f"登录提交没有被记下来：{posts}"
        assert posts[0]["fields"] == ["password", "username"], (
            f"登录日志只该记字段名，实际 {posts[0].get('fields')}"
        )
        assert "hunter2" not in str(posts), (
            "登录日志里出现了提交上来的值 —— 这份日志只该记『有谁提交过』，不该记提交了什么"
        )
        assert len(write_calls()) == before, (
            f"登录提交被算成了一次写操作：{write_calls()} —— 两份日志必须分开"
        )


def test_mock_never_verifies_credentials(mock: str) -> None:
    """★ 空凭据、乱填凭据都"成功" —— mock **不校验**任何凭据。

    这条是在守一条红线，不是守一个功能：
    这个 mock 存在的意义是"不需要真实账号也能测"，所以它必须
    在任何输入下都不产生"登录成功 → 拿到真数据"的语义。
    一旦有人给它加上凭据校验，下一步就会有人往 CI 里塞一个真账号。

    ★ 它同时保证了 e2e 可以**不登录**就跑通 —— 而"逼着测试去登录的夹具，
      会逼着某个人写出登录代码"。
    """
    with _client(mock) as c:
        for payload in ({}, {"username": "", "password": ""}, {"username": "x", "password": "y"}):
            assert c.post("/login", data=payload).status_code == 200
        # 而且登录与否**不影响**能不能拿到商品列表
        assert "共 7 件商品" in c.get("/goods/goods_list").text


def test_mock_does_not_mutate_its_data_on_write(mock: str) -> None:
    """★ 写端点只**记录**，不改内存里的数据 —— 这是刻意的，理由很实际。

    真删掉的话，第一个用例跑完之后，后面所有用例看到的商品列表都不一样了。
    而"每次跑都从同一份数据出发"是这个夹具唯一的价值来源。
    一个会随测试顺序变化的数据集，会让失败变成"取决于谁先跑" ——
    那种失败没法排查，因为单跑一次永远是绿的。

    护栏 e2e 断言的是**请求有没有到达**，而它到没到达和数据变没变无关。

    ★ 对照：先证明"删除"这个请求确实被记下了（否则"数据没变"可能只是
      "请求压根没发出去"）。
    """
    with _client(mock) as c:
        assert c.post("/goods/delete/100001").status_code == 200
        assert len(write_calls()) == 1, "对照：这次删除必须被记下来"
        assert c.get("/goods/detail/100001").status_code == 200, "商品被真的删掉了"
        assert len(filter_goods()) == len(GOODS), "内存里的商品数变了"


def test_running_server_resets_state_on_entry(mock: str) -> None:
    """★ 进服务器时清一次日志 —— 跨用例残留会让**对照实验说谎**。

    上一个用例留下的 True 能让这一个用例的"标志确实会变 True"通过，
    而这一次其实什么都没发生。一个被污染的对照实验比没有对照实验更坏 ——
    它给出的是一份**看起来被验证过**的结论。

    这里手工弄脏，然后起一台新服务器，断言新服务器是干净的。
    """
    with _client(mock) as c:
        c.post("/goods/pay")
    assert write_calls(), "对照：这一步必须先把日志弄脏"
    with running_server():
        assert write_calls() == [], "新起的服务器继承了上一台的日志"
        assert login_posts() == []


# ══════════════════════════════════════════════════════════
# 第四层：mock 的任务模板（纯编译，零浏览器）
# ══════════════════════════════════════════════════════════
def test_mock_task_yaml_allows_extract_table() -> None:
    """★★ 这份 YAML 修掉了一个真实存在的**静默失效**，这条测试是那个结论的可执行证据。

    规则引擎的四个维度是 AND，而 `match_element_text` 在拿不到元素文本时
    **判定为不命中**（guardrails/rules.py:117-124，刻意如此）。
    而 `extract_table` 是**不针对任何元素**的动作 —— 它没有元素文本可拿。

    两者相乘：任何同时写了 `match_action` 含 extract_table **和**
    `match_element_text` 的 allow 规则，**永远不可能匹配 extract_table**。
    它躺在 YAML 里、编译进 task_text、报告里显示"策略已加载" ——
    而它一次都没生效过。

    ★ 本用例两半都要有：
      上半段（正）：mock 模板里 allow-readonly 不带 match_element_text，
                    所以 extract_table 真的被放行；
      下半段（对照）：**先**证明"元素文本判据确实拦得住它" —— 用一条
                    合成规则（带 match_element_text 的 allow）判同一个动作，
                    必须**不是** allow。

    ★★ 下半段在 2026-09-17 被**有意改写过一次**，经过值得留着：

      原来它断言的是"真实存在的 pdd 模板里 extract_table 不是 allow" ——
      也就是说它绑在一个**当时的真实缺陷**上（pdd 的 allow-readonly 带着
      match_element_text，于是那条规则对 extract_table 永不命中）。

      缺陷修好之后（pdd 拆成了 allow-readonly-text / allow-readonly-noelement
      两条，后者不带文本判据），这条断言红了 —— 那是**对的**。
      但它红掉的同时也带走了"元素文本判据确实在起作用"这件事的证据，
      所以改成用合成规则来提供对照：覆盖一样，代价为零，
      而且不再把一条测试绑在"仓库里得有个缺陷"上。
    """
    from ecom_agent.dsl.compiler import compile_task
    from ecom_agent.dsl.loader import load_task
    from ecom_agent.guardrails.rules import Decision, GuardrailRule

    spec = load_task(PROJECT_ROOT / "tasks" / "mock_shop_readonly.yaml")
    compiled = compile_task(spec, {"keyword": ""})
    url = "http://127.0.0.1:12345/goods/goods_list"

    verdict = compiled.policy.evaluate("extract_table", {"table_index": 0}, url, None)
    assert verdict.decision.value == "allow", (
        f"mock 模板没有放行 extract_table（判定 {verdict.decision.value}，"
        f"规则={verdict.rule_id or 'default'}）—— "
        f"那么 e2e 里的 extract_table 会被要求人工确认，而没有人会去点它"
    )

    # ── 对照：同一个动作，换一条**带元素文本判据**的 allow 规则 → 判不出 allow ──
    #    ★ 这正是静默死条目的成因，所以对照必须直接构造它，而不是去真实模板里找 ——
    #      真实模板已经修好了，再去找就是在养着一个缺陷做测试。
    control_spec = spec.model_copy(deep=True)
    control_spec.guardrails.rules = [
        GuardrailRule(
            id="synthetic-text-gated-allow",
            decision=Decision.ALLOW,
            match_action=["extract_table"],
            match_url="*127.0.0.1*",
            match_element_text="anything",  # ← 这一行让它永不命中
            reason="合成的：证明元素文本判据确实拦得住无元素动作",
        )
    ]
    control = compile_task(control_spec, {"keyword": ""})
    control_verdict = control.policy.evaluate("extract_table", {"table_index": 0}, url, None)
    assert control_verdict.decision.value != "allow", (
        f"带 match_element_text 的 allow 规则竟然放行了 extract_table"
        f"（规则={control_verdict.rule_id}）—— 那么『拿不到元素文本判不命中』"
        f"这条结论不成立，docs/spikes.md 和 guardrail_design.md 都要改"
    )


def test_mock_task_guardrails_cover_the_real_buttons() -> None:
    """★ 用**真策略**判**真页面上真有的那几个按钮**。

    护栏过松 → 挡不住；护栏过严 → 把正常任务卡死。两个方向都要断言 ——
    只断言"危险按钮被拦住了"的制度下，一条 `match_action: [click]` +
    `decision: block` 的无条件规则能让测试全绿，而它会拦死一切。
    """
    from ecom_agent.dsl.compiler import compile_task
    from ecom_agent.dsl.loader import load_task
    from devtools.mock_pdd.pages import goods_list_page

    spec = load_task(PROJECT_ROOT / "tasks" / "mock_shop_readonly.yaml")
    policy = compile_task(spec, {"keyword": ""}).policy
    url = "http://127.0.0.1:12345/goods/goods_list"

    html = goods_list_page(
        page_of(list(GOODS), 1), keyword="", status="全部", page=1, pages=3, total=len(GOODS)
    )
    buttons = set(re.findall(r"<button[^>]*>([^<]+)</button>", html))
    assert {"批量删除", "立即支付", "搜索"} <= buttons, (
        f"mock 列表页上找不到预期的按钮，断言的前提不成立。实际：{sorted(buttons)}"
    )

    def judge(label: str) -> str:
        return policy.evaluate("click", {"index": 1}, url, label).decision.value

    assert judge("批量删除") == "block", "最危险的那个按钮没被拦"
    assert judge("立即支付") == "confirm", "资金类动作应当是 confirm（不是 block，也不是 allow）"
    assert judge("编辑") == "confirm", "写入意图的链接应当是 confirm"
    assert judge("搜索") == "allow", "只读的『搜索』被拦了 —— 护栏过严会把正常任务卡死"
    assert judge("下一页") == "allow", "只读的翻页被拦了"
    assert judge("查看详情") == "allow", "只读的查看详情被拦了"
