"""`report.html` 的渲染（纯函数，零文件系统、零浏览器）。

★ 这个文件目前只覆盖**登录态那一行**，不是因为它重要到别的都不用测，
  而是因为它守的是一件**别处守不住的事**：一次零行的 run，
  「店里没数据」和「落在登录页上按规矩停下汇报」在报告里长得一模一样
  （status=completed、parse_status=empty、0 行、截图齐全）。

  报告是**唯一**会被真人打开的那份产物 —— LLM 的中文 note 在 run.json 里，
  截图要一张张点开。所以"能不能一眼分辨"这件事，最终落在这一行上。

★★ 每条断言都配了对照，理由沿用这个项目的老规矩：
  只断言"出现了某句话"是不够的 —— 一个**永远**显示这句话的实现也能通过，
  而那种实现比没有这一行更坏（它会把一次正常的空结果也标成登录失效）。
"""
from __future__ import annotations

from ecom_agent.observability.models import COMPLETED, RunRecord
from ecom_agent.observability.report import render_report
from ecom_agent.runtime import loginstate as L

START = "https://mms.pinduoduo.com/goods/goods_list"
LOGIN_URL = "https://passport.pinduoduo.com/login"


def _record(**kw) -> RunRecord:
    base = dict(
        run_id="2026-01-01T00:00:00+00:00-abc123",
        task_id="pdd.shop_overview",
        task_name="店铺总览",
        status=COMPLETED,
        parse_status="empty",
        rows_collected=0,
        start_url=START,
    )
    base.update(kw)
    return RunRecord(**base)


def _html(**kw) -> str:
    return render_report(_record(**kw), [])


# ══════════════════════════════════════════════════════════
# 一、落在登录页：这是唯一需要喊出来的情况
# ══════════════════════════════════════════════════════════
def test_a_login_page_run_says_the_zero_rows_are_not_about_risk_control():
    """★★ 落在登录页时，报告必须自己说清"零行是这个原因，不是风控"。

    ★ 为什么这句话非要在报告里，而不是只写在日志里：
      日志是跑的时候看的，报告是**事后**（甚至第二天）看的。
      而这次失败最贵的代价不是零行，是**方向找错** —— 去查风控、去换账号、
      去怀疑解析逻辑，而真正该做的是重新扫一次码。
    """
    html = _html(
        login_state=L.LOGIN_PAGE,
        login_state_reason="URL 落在登录路径上：https://passport.pinduoduo.com/login",
        login_state_url=LOGIN_URL,
    )

    assert "登录态" in html, "这一行本身要在报告里"
    assert "落在登录页" in html
    assert "不是风控" in html, "要点名它不是风控 —— 这是最容易找错的方向"
    assert "login_pdd.py" in html, "要给出**可执行**的修复动作，不是一句'请重新登录'"
    assert "不作数" in html, "要说清这份报告的其余部分都因此不可信"
    # ★ 依据原样带上：判错时它是唯一能分清"脚本看错了"还是"真的没登录"的线索。
    assert "passport.pinduoduo.com" in html


def test_a_logged_in_run_does_not_cry_wolf():
    """★ 对照：同一个零行结果，登录态正常时**不许**出现上面那些话。

    没有这一条的话，一个"永远显示登录失效"的实现也能让上面那条通过 ——
    而那种实现会把每一次正常的空结果都染成"该去重新登录"，
    人扫完码回来发现还是零行，然后开始怀疑一切。
    """
    html = _html(
        login_state=L.LOGGED_IN,
        login_state_reason="页面上出现了登录后才有的导航：商品管理 @ " + START,
        login_state_url=START,
    )

    assert "已登录" in html
    assert "落在登录页" not in html
    assert "不是风控" not in html, "登录正常时喊'不是风控'就是纯噪声"
    assert "login_pdd.py" not in html, "没坏就别给修复指引 —— 那会让人以为坏了"


# ══════════════════════════════════════════════════════════
# 二、两种沉默必须说成两句不同的话
# ══════════════════════════════════════════════════════════
def test_not_probed_and_unknown_read_as_two_different_sentences():
    """★★ `""`（没探）和 `unknown`（探了但看不清）在报告里必须是两句话。

    这两种沉默的处置**完全相反**：前者一切正常（这个任务不需要登录态），
    后者需要人去看一眼截图。压成同一句话的话，报告就没法说清它到底是哪种沉默 ——
    而"说不清"正是这一行存在的理由。

    ★ 断言落在**取值单元格**上（`<td>…`），不是"整份 HTML 里有没有这个词"。
      因为『未探测』那一行的说明里**故意**提了一句"它和『探了但看不清』是两回事" ——
      那句话正是这两行必须分开的理由，不该被自己的测试判成违规。
      ⚠️ 第一版就是按整份 HTML 写的，于是它红在了一句**对的**文案上。
    """
    untouched = _html(login_state=L.NOT_PROBED)
    assert "<td>未探测" in untouched
    assert "<td>看不清" not in untouched, "没探过却把取值说成'看不清'，会让人去查一件没发生的事"
    assert "不需要登录态" in untouched, "要说清为什么没探（否则它看起来像'我们忘了探'）"

    unclear = _html(login_state=L.UNKNOWN, login_state_reason="两边都不像（正文 12 字符）")
    assert "<td>看不清" in unclear
    assert "<td>未探测" not in unclear
    assert "两边都不像" in unclear, "依据要原样带上"
    assert "落在登录页" not in unclear, (
        "★★ 看不清时**绝不能**倒向'没登录' —— 那个方向的代价是零行 + "
        "一句把人引去查风控的话，而反方向只是白扫一次码"
    )


# ══════════════════════════════════════════════════════════
# 三、位置与细节
# ══════════════════════════════════════════════════════════
def test_the_login_row_comes_before_the_guardrail_rows():
    """★ 它必须排在『可信度』的**第一行** —— 位置是设计的一部分，不是排版。

    它否掉的是**整份报告**（零行 + 其余内容都不作数），而护栏那几行否掉的
    只是某几行数据。让人先读到"这份报告整体不可信"，再看细节。

    ★ 把顺序钉成一条断言，是因为**注释拦不住移动**：挪下去之后报告照样能渲染、
      测试照样能绿（原先没有任何测试碰过这个函数），
      只有"读报告的人在错的地方开始读"这一个后果，而那要等到真的出事才发现。
    """
    html = _html(
        login_state=L.LOGIN_PAGE,
        login_state_reason="URL 落在登录路径上",
        login_state_url=LOGIN_URL,
    )
    assert html.index("登录态") < html.index("护栏判定"), (
        "登录态那一行被挪到护栏下面去了 —— 它就不再是'先看这块'里先看到的东西"
    )


def test_the_jump_is_only_mentioned_when_there_was_one():
    """★ "实际落到的 URL 与任务起点不同"是**跳转**的直接证据 —— 但只在真跳了时才说。

    起点和终点相同时多写这一句，会让人去读一个不存在的差异；
    而真正跳转的那次（最常见的情形）反而看不出区别在哪。
    """
    jumped = _html(
        login_state=L.LOGIN_PAGE, login_state_reason="URL 落在登录路径上", login_state_url=LOGIN_URL
    )
    assert "发生了跳转" in jumped and LOGIN_URL in jumped

    same = _html(
        login_state=L.LOGGED_IN, login_state_reason="命中商品管理", login_state_url=START
    )
    assert "发生了跳转" not in same, "没跳却说跳了 —— 那是编出来的证据"


def test_the_verdict_is_never_rendered_as_a_raw_enum():
    """★ 报告是给人看的，别把内部枚举（`login_page`）直接怼出来。

    真出现原始值时，看起来像"渲染漏了"而不是"我们看见了什么" ——
    而报告里出现一个没人认识的词，最可能的后果是被当成噪声跳过。
    """
    for verdict, label in (
        (L.LOGIN_PAGE, "落在登录页"),
        (L.LOGGED_IN, "已登录"),
        (L.UNKNOWN, "看不清"),
        (L.NOT_PROBED, "未探测"),
    ):
        html = _html(login_state=verdict, login_state_reason="依据")
        assert label in html, f"{verdict} 没有渲染成人话"
        if verdict:
            assert f">{verdict}<" not in html, f"{verdict} 把内部枚举原样渲染出来了"
