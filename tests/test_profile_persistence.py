"""真浏览器：登录态能不能**跨会话**留下来（Phase 6 的承重机制）。

★ 为什么这个文件必须存在（它是 Phase 6 唯一"必须真浏览器"的验收）：

  整个"人工扫码一次、之后复用 profile"的设计压在一个**未文档化且反直觉**的
  库行为上：`BrowserProfile.model_post_init` 会在构造时把 user_data_dir
  **拷到一个临时目录**再启动（`_copy_profile()`），并把
  `self.user_data_dir` 换成那个临时目录。

  后果不是报错，是**登录态写不回原目录**：
      人工扫码 → 脚本报成功 → cookie 落在 %TEMP% 里随进程消失
      → 下一次 run 看到登录页 → 按任务文本"遇登录页立即停止"收场
      → 退出码 0、报告齐全、**零行数据**。一条完全静默的失败。

  所以这里用真浏览器证明两件事，而且**用同一套动作、只改一个变量**（钉/不钉）：

      钉住   → cookie 写进我们的目录，新会话读得回来   （主判据）
      不钉   → cookie 写进库的临时目录，新会话读不到   （对照实验）

  第二条不是"多余的反例"：没有它，第一条在"cookie 其实写哪都能留下"的
  世界里也会绿 —— 那种绿什么都证明不了。

★ 对照路径必须**共用同一套关闭动作**（`close_gracefully_and_flush`），
  否则就变成两个变量了。踩过：第一版对照组用的是 `kill()`，结果它失败的原因
  是"强杀丢 cookie"而不是"目录不对"，两条路都红，实验什么也没说明。
  （顺带那就是另一个坑：cookie 不是写一次落一次盘，详见 browser.py。）

★ 关于"钉不住"的另一半（在 profile.py 里）：cookie 落盘还依赖**优雅关闭**。
  库里 `session.stop()` 不是优雅退出（实测库里 0 行）；能用的是 CDP 的
  `Browser.close`。这两件事任一失效，下面的主判据都会红。
"""
from __future__ import annotations

import asyncio
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from ecom_agent.config import CHROME_PATH
from ecom_agent.runtime.browser import close_gracefully_and_flush, kill_quietly, pin_user_data_dir

pytestmark = pytest.mark.needs_browser

# 远程调试端口之外的等待都用它，别在测试里各写各的。
# ★ 2.0s 不是随手取整：实测「优雅关闭后 cookie 立刻在盘上」就是在这个配置下测的
#   （见 browser.py:close_gracefully_and_flush 的 docstring）。测试跟着实测配置走 ——
#   为了快 1 秒把它调小，换来的是一条会随机红的用例，那比多等 4 秒贵得多。
SETTLE_S = 2.0

SET_HTML = (
    "<html><body>set<script>"
    # ★ 必须带 expires：会话 cookie（不带 expires）Chrome 只在内存里，
    #   退出即丢 —— 那样测出来的是"会话 cookie 丢了"，不是"目录不对"。
    #   真实站点的登录 cookie 也都是持久 cookie，所以这条也更贴近实际。
    "document.cookie='ecom_probe=1; path=/; expires=Tue, 31 Dec 2099 23:59:59 GMT';"
    "</script></body></html>"
)


@pytest.fixture
def site(tmp_path: Path):
    """一个只服务两个静态页的本机站点（零网络、零 token）。"""
    (tmp_path / "set.html").write_text(SET_HTML, encoding="utf-8")
    (tmp_path / "get.html").write_text("<html><body>get</body></html>", encoding="utf-8")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def log_message(self, *args):  # 静音：测试输出里不需要 HTTP 访问日志
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


async def _open(profile: Path, *, pin: bool):
    from browser_use import BrowserSession

    session = BrowserSession(
        headless=True,
        executable_path=CHROME_PATH or None,
        user_data_dir=str(profile),
        keep_alive=False,
    )
    if pin:
        pin_user_data_dir(session, profile)
    await session.start()
    return session


async def _cookie(session) -> str:
    page = await session.get_current_page()
    assert page is not None, "会话里没有页面"
    return await page.evaluate("(arg) => document.cookie", None)


async def _write_cookie_then_close(profile: Path, port: int, *, pin: bool) -> str:
    """开一个会话 → 让页面写一个 cookie → 优雅关闭。返回"钉住之后实际用的目录"。"""
    session = await _open(profile, pin=pin)
    try:
        await session.navigate_to(f"http://127.0.0.1:{port}/set.html")
        await asyncio.sleep(SETTLE_S)
        assert "ecom_probe=1" in await _cookie(session), "页面没能写上 cookie（测量前提不成立）"
        used = str(session.browser_profile.user_data_dir)
        await close_gracefully_and_flush(session, settle_s=SETTLE_S)
        return used
    except BaseException:
        await kill_quietly(session)
        raise


async def _read_cookie_in_fresh_session(profile: Path, port: int) -> str:
    session = await _open(profile, pin=True)
    try:
        await session.navigate_to(f"http://127.0.0.1:{port}/get.html")
        await asyncio.sleep(SETTLE_S)
        return await _cookie(session)
    finally:
        await close_gracefully_and_flush(session, settle_s=SETTLE_S)


async def test_pinned_profile_keeps_cookies_across_sessions(tmp_path: Path, site: int):
    """★ 主判据：钉住 user_data_dir 之后，登录态能跨会话留下来。"""
    profile = tmp_path / "profile"
    profile.mkdir()

    used = await _write_cookie_then_close(profile, site, pin=True)
    assert Path(used) == profile, (
        f"钉不住：会话实际用的是 {used}，不是我们给的 {profile}。\n"
        "库的 _copy_profile() 又生效了 —— 登录状态永远写不回原目录。\n"
        "（若这是库升级带来的行为变化，先读 runtime/browser.py:pin_user_data_dir 的长注释。）"
    )

    got = await _read_cookie_in_fresh_session(profile, site)
    assert "ecom_probe=1" in got, (
        f"新会话读不到 cookie（拿到 {got!r}）—— 登录态没能留下来。\n"
        "这正是 Phase 6 最怕的那条静默失败：扫码看起来成功了，之后每次 run 都停在登录页。"
    )


async def test_without_pinning_the_cookie_goes_to_the_librarys_temp_dir(tmp_path: Path, site: int):
    """★★ 对照实验：只改"钉不钉"这一个变量，登录态就没了。

    ★ 如果这条**突然绿了**（即不钉也能留下），那不是好消息也不是坏消息，
      而是"库自己改掉了 _copy_profile 的行为"的信号 ——
      那时该做的是去看 pin_user_data_dir 还需不需要，而不是删掉这条测试。
      断言里把这件事写出来了，免得下一个人把它当成 flaky 直接删。
    """
    profile = tmp_path / "profile"
    profile.mkdir()

    used = await _write_cookie_then_close(profile, site, pin=False)
    assert Path(used) != profile, (
        "不钉的时候库居然用了我们给的目录 —— 这版库的 _copy_profile 行为变了，"
        "去看 runtime/browser.py:pin_user_data_dir 还需不需要保留（别直接删本用例）"
    )

    # ⚠️ 顺序要紧：这条必须在下面的复核**之前**查 ——
    #    复核那一步（pin=True）自己就会在我们的目录里建出 Default/，
    #    放后面查就变成"断言自己制造出来的东西不存在"。
    assert not (profile / "Default").exists(), (
        "我们的目录里出现了 Chrome 的 profile 子目录 —— 说明写 cookie 那一次"
        "其实是在我们的目录里跑的，上一条断言需要重新解释"
    )

    got = await _read_cookie_in_fresh_session(profile, site)
    assert "ecom_probe=1" not in got, (
        f"不钉也留下了 cookie（拿到 {got!r}）—— 对照组失去意义，"
        "说明「钉 user_data_dir」不再是登录态成立的必要条件"
    )
