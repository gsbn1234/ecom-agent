"""`runtime/loginstate.py`：登录态判定与探测（全离线，零浏览器）。

★ 这四条 `classify` 的用例原本住在 `tests/test_login_script.py` 里，现在**跟着代码
  一起搬过来了** —— 判定逻辑从 `devtools/login_pdd.py` 移进了库
  （`ecom_agent/runtime/loginstate.py`），因为 run 也要用它。测试跟着被测代码走，
  否则下一次重构时"谁该改"就没有答案。

★★ 这个模块存在的理由是一条**产物无法自证**的失败：空数据导致的零行，和登录态
  失效导致的零行，在 run.json / 报告 / 退出码里**长得一模一样**。所以这里的
  每条断言都在守"能不能分辨"，而不是"功能对不对"。
"""
from __future__ import annotations

import pytest

from ecom_agent.runtime import loginstate as L

PASSPORT = "https://passport.pinduoduo.com/login"
GOODS = "https://mms.pinduoduo.com/goods/goods_list"


# ══════════════════════════════════════════════════════════
# 一、classify：三值判定（从 test_login_script.py 搬来）
# ══════════════════════════════════════════════════════════
def test_a_login_url_beats_page_text():
    """★ URL 落在登录路径上时，**页面文字不作数**。

    ★ 对照在同一用例里取：同样的页面文字、换一个正常 URL → 判成 logged_in。
      没有这一半的话，一个"永远返回 login_page"的实现也能让上面通过，
      而那种实现的表现是"扫完码脚本还说没登录"，人只会以为码扫失败了。
    """
    dom = "商品管理 订单管理 扫码登录"
    assert L.classify(PASSPORT, dom)[0] == L.LOGIN_PAGE
    assert L.classify(GOODS, dom)[0] == L.LOGGED_IN


def test_a_neutral_page_is_unknown_not_a_guess():
    """★★ 判不出来时必须是 unknown —— **不能**倒向任何一边。

    这一条守的是失败方向：把"没登录"错报成"登录了"，后果是零行数据 + 一句
    把人引去查风控的"需要人工登录"；而把"登录了"错报成"没登录"，
    后果只是白扫一次码。两个方向的代价差得很远，所以**不猜**。

    ★ 对照：同一页面上加一个登录后才有的词 → 必须判成 logged_in。
      没有这一半，"永远返回 unknown"的实现也能让上面通过。
    """
    verdict, why = L.classify(GOODS, "正在加载…")
    assert verdict == L.UNKNOWN, f"看不清却给了结论：{verdict}（{why}）"
    assert verdict != L.LOGIN_PAGE, "判不出来时倒向'没登录'是错的方向 —— 它会让人白扫一次码"

    assert L.classify(GOODS, "正在加载… 商品管理")[0] == L.LOGGED_IN


def test_login_hints_alone_are_enough_to_say_login_page():
    """只有一个词也算数：这是"人还没扫"的正常状态（页面停在扫码登录）。"""
    assert L.classify(GOODS, "请扫码登录")[0] == L.LOGIN_PAGE


def test_the_reason_carries_the_evidence_the_human_needs():
    """依据要**原样打给人看**，所以它必须自己就能解释结论。

    ★ 判错的时候，唯一能让人快速分清"脚本看错了"还是"真的没登录"的就是它。
      于是：命中了哪些词、看的是哪个 URL，都要在里面。
    """
    _, why = L.classify(GOODS, "请扫码登录")
    assert "扫码登录" in why, "要说清命中了哪个词"
    assert GOODS in why, "要说清是在哪个 URL 上判的"

    # ★ 另一半：判成 logged_in 时同样要说清命中了哪个词。
    #   ⚠️ 第一次写这条测试时我把两半写反了（用一个同时含两类词的页面去断言
    #   "依据里该有登录词"）—— 而 classify 是先看登录后才有的词的，
    #   所以依据里出现的是"商品管理"。测试红了，红得对：它暴露的是我的预期错了，
    #   不是代码错了。两半分开写才不会有这种歧义。
    _, why_in = L.classify(GOODS, "商品管理")
    assert "商品管理" in why_in, "判成已登录时也要说清凭哪个词判的"

    _, why = L.classify(GOODS, "空的")
    assert GOODS in why
    assert "unknown" not in why, "依据是给人看的，别把内部枚举塞进去"


# ══════════════════════════════════════════════════════════
# 二、三个取值必须彼此可分
# ══════════════════════════════════════════════════════════
def test_not_probed_and_unknown_are_different_silences():
    """★★ `""`（没探）和 `"unknown"`（探了但看不清）**必须不是同一个值**。

    压成一个的话，报告就没法说清它到底是哪种沉默：前者是"这个任务不需要登录态"
    （一切正常），后者是"我们看不清这一页"（需要人去看）。这两种沉默的**处置
    完全相反**，却在产物里长得一样 —— 正是这个模块要消灭的那类问题。
    """
    assert L.NOT_PROBED != L.UNKNOWN
    assert L.LoginState().verdict == L.NOT_PROBED
    assert "未探测" in L.LoginState().describe()

    probed = L.LoginState(L.UNKNOWN, "看不清")
    assert "未探测" not in probed.describe()
    assert "看不清" in probed.describe()


def test_only_a_confirmed_login_page_raises_the_flag():
    """★ `is_login_page` 只在一个方向上为真。

    它是代码里唯一会因此**说话**（logger.warning）的判据。让 unknown 也算进去，
    等于每次看不清都喊一次"零行是因为登录"——那会把人训练成忽略这句话，
    而这句话恰恰是这条链上唯一能省下一次白跑的东西。
    """
    assert L.LoginState(L.LOGIN_PAGE, "x").is_login_page is True
    assert L.LoginState(L.UNKNOWN, "x").is_login_page is False
    assert L.LoginState(L.LOGGED_IN, "x").is_login_page is False
    assert L.LoginState().is_login_page is False


# ══════════════════════════════════════════════════════════
# 三、probe：它绝不能把 run 弄挂
# ══════════════════════════════════════════════════════════
class _StubSession:
    """按剧本回放若干次 (url, 文本)；剧本用完后一直重复最后一个。

    ★ 只实现 `get_browser_state_summary` —— 被测代码用到别的会 AttributeError 地
      **当场炸**，那是想要的：它说明 probe 开始依赖我没想到的东西了。
    """

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self._script = script
        self.reads = 0

    async def get_browser_state_summary(self, *, include_screenshot: bool = False):
        assert include_screenshot is False, "探测不需要截图，别顺手把视觉通道开起来"
        item = self._script[min(self.reads, len(self._script) - 1)]
        self.reads += 1
        return _Summary(*item)


class _DomState:
    def __init__(self, text: str) -> None:
        self._text = text

    def llm_representation(self) -> str:
        return self._text


class _Summary:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.dom_state = _DomState(text)


async def test_probe_reads_the_landed_page(no_sleep):
    """★ 主判据：落在登录页时探出来就是登录页。

    ★ 对照在同一用例里取：同一段代码、换一个正常页面 → 必须报 logged_in。
      没有这一半，"永远返回 login_page"的实现也能让上面通过 ——
      而那种实现会让每一次真站点 run 都喊一声假警报。
    """
    got = await L.probe(_StubSession([(PASSPORT, "请扫码登录")]))
    assert got.verdict == L.LOGIN_PAGE
    assert got.is_login_page is True
    assert PASSPORT in got.reason, "依据里要有 URL —— 判错时它是唯一能自证的线索"
    assert got.url == PASSPORT

    ok = await L.probe(_StubSession([(GOODS, "商品管理 订单管理")]))
    assert ok.verdict == L.LOGGED_IN
    assert ok.is_login_page is False


async def test_probe_waits_out_a_redirect_but_only_when_it_cannot_tell(no_sleep):
    """★★ 跳转中间态要等，但**只在判不出来的时候等**。

    第一次读到的是"正在加载…"（两边都不像）→ 第二次才是登录后的页面。
    没有这个重看，run 会把一个正常的、只是还没跳完的页面记成 unknown ——
    于是"能不能分辨"这件事在最需要它的时刻（跳转慢的那次）恰好失效。

    ★ 另一半同样重要：**判出来了就不许再读**。无条件等 3 秒是拿每一次 run 的
      时间去买一个少数情况，而且会把这个字段变成"每次都很慢"的东西。
    """
    session = _StubSession([(GOODS, "正在加载…"), (GOODS, "商品管理")])
    got = await L.probe(session)
    assert got.verdict == L.LOGGED_IN, f"没等到跳转完成就下结论：{got.describe()}"
    assert session.reads == 2, f"该重看一次（实际读了 {session.reads} 次）"

    settled = _StubSession([(GOODS, "商品管理"), (GOODS, "商品管理")])
    assert (await L.probe(settled)).verdict == L.LOGGED_IN
    assert settled.reads == 1, "第一次就判出来了，却又读了一次 —— 这是白等"


async def test_probe_never_raises_even_if_the_session_is_dead(no_sleep):
    """★★ 探测是**诊断**，不是运行的一部分 —— 它绝不允许把 run 弄挂。

    会话读不到（窗口被关了 / CDP 断了）时返回 unknown 并把异常写进依据，
    而不是抛出去：一次探测失败不该让一次**本来能跑完**的 run 崩掉。
    """

    class _Dead:
        async def get_browser_state_summary(self, **_kw):
            raise RuntimeError("Target closed")

    got = await L.probe(_Dead())
    assert got.verdict == L.UNKNOWN
    assert "Target closed" in got.reason, "要把异常原样带上，否则排查时不知道发生了什么"
    assert got.is_login_page is False, "读不到 ≠ 落在登录页 —— 不能倒向那个方向"


@pytest.fixture
def no_sleep(monkeypatch):
    """把重看之间的等待换掉，让测试快。

    ★ 换的是这个模块里的 `asyncio` 名字，所以只在本模块的路径上安全：
      被测代码若用到 asyncio 的别的东西，会 AttributeError 地**当场炸**，
      而不是静默地少等一会儿。
    """
    from types import SimpleNamespace

    async def _no_wait(_s):
        return None

    monkeypatch.setattr(L, "asyncio", SimpleNamespace(sleep=_no_wait))
