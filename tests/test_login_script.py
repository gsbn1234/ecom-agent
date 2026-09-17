"""登录脚本的两块决策逻辑（纯离线，零浏览器、零网络）。

★ 为什么给一个 `devtools/` 下的、人工跑一次就完的脚本写测试：

  因为 `login_pdd.py` 里有一句**决定 Phase 6 成败的话**："复核通过才写标记"。
  如果这条不成立，人工扫码之后脚本会报成功，而之后**每一次 run 都停在登录页**、
  报"需要人工登录"、零行数据 —— 而线索（标记文件）看起来完全正常。
  换句话说：脚本报的"成功"必须是**它自己验过的**成功，这条性质值得被测。

★ 判定那一半（`classify()` 的三值语义）已随代码搬去 `tests/test_loginstate.py`
  —— 判定逻辑从本脚本移进了库（`ecom_agent/runtime/loginstate.py`），因为 run
  也要用它来记录"这次到底登进去没有"。本文件现在只管**脚本自己的决策链**。

★ 这一整个文件都不开浏览器：`run()` 的三处外部依赖（人、开浏览器、优雅关闭）
  全部被替换掉了，测的是**决策链**。
"""
from __future__ import annotations

from types import SimpleNamespace

from devtools import login_pdd
from ecom_agent.runtime.profile import marker_path, read_mark

PASSPORT = "https://passport.pinduoduo.com/login"
GOODS = "https://mms.pinduoduo.com/goods/goods_list"


# ══════════════════════════════════════════════════════════
# run()：写不写标记这件事
# ══════════════════════════════════════════════════════════
class _DomState:
    def __init__(self, text: str) -> None:
        self._text = text

    def llm_representation(self) -> str:
        return self._text


class _Summary:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.dom_state = _DomState(text)


class _FakeSession:
    """只实现 `snapshot()` 用到的那两件事。★ 不实现别的 —— 用到了会 AttributeError，
    那是**想要**的：它说明被测代码开始依赖我没想到的东西了。"""

    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self._text = text
        self.navigated: list[str] = []

    async def navigate_to(self, url: str) -> None:
        self.navigated.append(url)

    async def get_browser_state_summary(self, *, include_screenshot: bool = False) -> _Summary:
        assert include_screenshot is False, "复核不需要截图，别顺手把视觉通道也开起来"
        return _Summary(self.url, self._text)


def _prepare(monkeypatch, tmp_path, *, page_url: str, dom_text: str, human_ok: bool = True):
    """把 run() 的外部依赖全换掉，返回 (profile 目录, 参数, 调用记录)。"""
    profile = tmp_path / "profile"
    args = login_pdd.build_parser().parse_args(
        ["--profile", str(profile), "--url", "https://mms.pinduoduo.com/"]
    )
    # CI 环境变量会让脚本直接退出 2（那是给 CI 的保护）。测决策链时先摘掉它。
    monkeypatch.delenv("CI", raising=False)

    calls: list[str] = []

    async def fake_phase_a(_profile, _url, _timeout):
        calls.append("phase_a")
        return human_ok, "假的：人扫码成功" if human_ok else "假的：等超时了"

    session = _FakeSession(page_url, dom_text)

    async def fake_open(_profile, *, headless):
        calls.append(f"open(headless={headless})")
        return session

    async def fake_close(_session, **_kw):
        calls.append("close")
        return True

    async def no_wait(_seconds):
        # ★ 复核里有一次 3s 的等待。换掉它只为了让测试快 ——
        #   注意这是把整个 `asyncio` 名字换掉了，所以只在这个用例的路径上安全：
        #   万一被测代码用到 asyncio 的别的属性，会 AttributeError 地**当场炸**，
        #   而不是静默地少等一会儿。monkeypatch 结束时原样还原。
        return None

    monkeypatch.setattr(login_pdd, "phase_a_human_login", fake_phase_a)
    monkeypatch.setattr(login_pdd, "_open_with_retry", fake_open)
    monkeypatch.setattr(login_pdd, "close_gracefully_and_flush", fake_close)
    monkeypatch.setattr(login_pdd, "asyncio", SimpleNamespace(sleep=no_wait))
    return profile, args, calls


async def test_a_failed_fresh_session_check_writes_no_mark(monkeypatch, tmp_path):
    """★★ 本文件最重要的一条：**复核没通过就绝不写标记**。

    构造的正是最阴的那种情况：**人工阶段是成功的**（人真的扫码了），
    而新会话里却看不到登录态（cookie 没留下 / 落到了临时目录）。
    这时脚本必须返回 1 并且**一个字节都不写** ——
    写下去就等于制造"以为登录成功了"的假象，而那个假象要等几天后
    以"零行数据"的形式暴露，届时现场已经没了。
    """
    profile, args, _ = _prepare(
        monkeypatch, tmp_path, page_url=PASSPORT, dom_text="请扫码登录"
    )
    assert await login_pdd.run(args) == 1
    assert not marker_path(profile).exists(), (
        "新会话复核没通过却写了标记 —— 这会让之后每次 run 都静默地零行数据"
    )


async def test_a_passed_fresh_session_check_writes_the_mark(monkeypatch, tmp_path):
    """★ 对照：复核通过时必须写，且写的是**复核那一刻的 URL**（不是我们请求的 URL）。

    为什么强调这一点：真站点登录后会跳转（到这里或到某个首页）。标记里记的
    应该是**实际看到的那一页** —— 它才是"登录态长什么样"的证据。
    """
    profile, args, calls = _prepare(
        monkeypatch, tmp_path, page_url=GOODS, dom_text="商品管理 订单管理"
    )
    assert await login_pdd.run(args) == 0

    mark = read_mark(profile)
    assert mark is not None, "复核通过了却没写标记"
    assert mark.logged_in_url == GOODS, "记的应该是复核时实际看到的 URL"
    assert mark.verified_by == "fresh_session", "标记要说明它是被复核过的，不是扫码就写的"
    assert any(c.startswith("open(") for c in calls), "复核必须真的**新开一个**会话"


async def test_a_failed_human_phase_never_even_reaches_the_fresh_check(monkeypatch, tmp_path):
    """★ 人工阶段没成功时，连复核都不该做（更不该写标记）。

    断言的是"复核没被调用"，而不只是"退出码非 0"：一个先复核再判断的实现
    同样会返回 1，但它白开了一个浏览器 —— 而这里每一次多余的浏览器启动，
    都是一次可能撞上风控的请求。
    """
    profile, args, calls = _prepare(monkeypatch, tmp_path, page_url=GOODS, dom_text="商品管理",
                                    human_ok=False)
    assert await login_pdd.run(args) == 1
    assert not marker_path(profile).exists()
    assert not any(c.startswith("open(") for c in calls), f"人工阶段就没成功，却还去开浏览器复核：{calls}"
