"""人工扫码登录一次 → 把登录态留在持久 profile 里。**agent 永远不碰登录表单。**

★★ 这个脚本真正的交付物不是「二维码被扫了」，而是一句**可复核的断言**：

    「用这个目录**新开一个**浏览器，我还是登录状态。」

  为什么"扫码成功了"不能当交付物：那一刻的页面可能还在跳转、可能还有二次验证，
  而且 —— 这是最要命的一条 —— **cookie 可能还没落盘**（见 runtime/browser.py
  的 close_gracefully_and_flush，那是实测出来的）。所以本脚本分三步，
  只有第三步过了才算成功：

    第 1 步  开一个**有窗口**的浏览器，导航到商家后台，然后**什么都不做** ——
             人扫码、人点确认，脚本只负责看你什么时候登录好。
    第 2 步  **优雅关闭**（CDP Browser.close，不是 kill）让 cookie 落盘。
    第 3 步  **新开一个会话**（同一个 profile 目录），再导航一次，**没有任何人工操作**，
             看它还是不是登录态。这一步才是"下一次 run 会遇到的真实情形"。

  第 3 步不过就如实报失败，并且**不写登录标记** —— 于是 `ecom-agent run` 会用前检查
  会说出"这个 profile 没有登录标记"。宁可现在红，也不要让人明天去查风控。

★★ 为什么不自动登录（这是**设计**，不是能力不足）：

  1. 红线：不写任何反检测/拟人化/验证码绕过代码。把 RPA 伪装得更像真人，
     从合规角度反而更危险（深圳某科技公司案的争议焦点正是这类行为）。
  2. 风控避险（计划里的 R2）：扫码由人在真实窗口里完成，是最不像自动化的路径。
  3. 凭据根本不进 Agent：密码不出现在提示词里、不出现在截图里、不需要脱敏代码。
     这比 `sensitive_data` 占位符替换更硬 —— 后者是"把密码交给程序保管"，
     前者是"程序从来没见过密码"。

★★ 轮询期间**不刷新页面**：

  脚本每 3 秒读一次状态，读的是**本机 CDP 里的 DOM 快照**，不产生任何对拼多多的
  新请求 —— 所以这个等待过程本身不构成风控信号。⚠️ 如果你把它改成
  `page.reload()` 轮询，它就变成"每 3 秒请求一次登录页"，那是真会招风控的。
  这两件事长得像，性质完全不同。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ecom_agent.config import (  # noqa: E402
    CHROME_PATH,
    DEFAULT_PROFILE_DIR,
    HEADLESS,
    USER_DATA_DIR,
)
from ecom_agent.runtime.browser import (  # noqa: E402
    close_gracefully_and_flush,
    kill_quietly,
    pin_user_data_dir,
)
from ecom_agent.runtime.loginstate import (  # noqa: E402
    LOGGED_IN,
    classify,
    read_page_state,
)
from ecom_agent.runtime.profile import (  # noqa: E402
    ProfileMarkError,
    read_mark,
    write_mark,
)

logger = logging.getLogger("login_pdd")

PDD_HOME = "https://mms.pinduoduo.com/"
SITE = "mms.pinduoduo.com"

POLL_INTERVAL_S = 3.0
BEAT_EVERY_S = 15.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def open_session(profile: Path, *, headless: bool):
    """开一个会话，并把 profile 目录**钉住**（否则库会拷到临时目录，见 browser.py）。"""
    from browser_use import BrowserSession

    # ★ 刻意**不传** allowed_domains：这个脚本自己不发出任何动作，驱动浏览器的是人。
    #   白名单的职责是"约束 agent 能去哪"，在这里它只会帮倒忙 ——
    #   登录流程可能合法地跳几个域（passport.* 之类），一条白名单只会让登录
    #   以一个看不懂的"导航被拦"结束。这里的安全机制是**人**，不是列表。
    session = BrowserSession(
        headless=headless,
        executable_path=CHROME_PATH or None,
        user_data_dir=str(profile),
        keep_alive=False,
    )
    pin_user_data_dir(session, profile)
    await session.start()
    return session


async def wait_for_login(session, *, timeout_s: float) -> tuple[str, str]:
    """轮询等人在窗口里登录好。返回 (判定, 依据)。"""
    started = time.monotonic()
    last_beat = 0.0
    verdict, why = "unknown", "还没开始看"
    while time.monotonic() - started < timeout_s:
        try:
            url, dom_text = await read_page_state(session)
        except Exception as exc:  # noqa: BLE001
            # 窗口被人关掉了 / 会话断了。这不是"登录失败"，是"没法再看了"。
            return "unknown", f"读取页面状态失败（窗口关了？）：{type(exc).__name__}: {exc}"
        verdict, why = classify(url, dom_text)

        if verdict == LOGGED_IN:
            return verdict, why

        elapsed = time.monotonic() - started
        if elapsed - last_beat >= BEAT_EVERY_S:
            last_beat = elapsed
            print(f"    …已等 {int(elapsed)}s，当前判定：{verdict}（{why}）", flush=True)
        await asyncio.sleep(POLL_INTERVAL_S)

    return verdict, f"等满 {int(timeout_s)}s 仍未登录。最后一次判定依据：{why}"


async def phase_a_human_login(profile: Path, url: str, timeout_s: float) -> tuple[bool, str]:
    """第 1+2 步：开窗给人登录 → 优雅关闭让 cookie 落盘。"""
    print(f"① 打开浏览器（有窗口）→ {url}")
    print("   请在弹出的窗口里扫码登录。脚本不会替你点任何东西，也不用回来按回车。", flush=True)
    session = await open_session(profile, headless=False)
    closed = False
    try:
        await session.navigate_to(url)
        verdict, why = await wait_for_login(session, timeout_s=timeout_s)
        print(f"   判定：{verdict} —— {why}")

        if verdict != LOGGED_IN:
            # ★ 判不出来时留一张图，让人自己看一眼。这是"判不出来"唯一的出路：
            #   我们看不清，就把画面交给人，而不是猜一个结论。
            shot = profile / f"login_unclear_{datetime.now().strftime('%Y%m%dT%H%M%S')}.png"
            try:
                await session.take_screenshot(path=str(shot))
                print(f"   已存一张当时的截图，你可以自己看一眼：{shot}")
            except Exception as exc:  # noqa: BLE001
                print(f"   （截图也失败了：{type(exc).__name__}: {exc}）")
            return False, why

        print("② 优雅关闭浏览器（让 cookie 真正落盘 —— 强杀会丢掉它们）", flush=True)
        await close_gracefully_and_flush(session)
        closed = True
        return True, why
    finally:
        if not closed:
            await kill_quietly(session)


async def _open_with_retry(profile: Path, *, headless: bool, attempts: int = 2, wait_s: float = 4.0):
    """开浏览器，失败就等一会儿再试一次。

    ★ 为什么这里值得一次重试：钉住 profile 之后，**同一个 profile 目录不能被两个
      Chrome 同时用**（独占锁）。第 2 步刚关掉的那个 Chrome 可能还没退干净，
      于是第 3 步开不起来。这时报"profile 被占用"完全指不到真正的原因，
      而人刚刚才扫完码 —— 一次重试比一段解释便宜得多。
    ★ 但只重试一次、而且**每次都说话**：静默重试会让"其实一直起不来"看起来像"慢"。
    """
    for attempt in range(1, attempts + 1):
        try:
            return await open_session(profile, headless=headless)
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts:
                raise
            print(f"   开浏览器失败（第 {attempt} 次）：{type(exc).__name__}: {exc}")
            print(f"   可能是上一个 Chrome 还没退干净（profile 独占锁）。等 {wait_s:.0f} 秒再试。")
            await asyncio.sleep(wait_s)


async def phase_b_verify_fresh(profile: Path, url: str) -> tuple[bool, str, str]:
    """第 3 步：**新会话**复核。返回 (是否通过, 判定依据, 实际 URL)。

    ★ headless 用 config.HEADLESS（= 将来 run 的模式）。这是刻意的：
      要复核的是"**run 会看到什么**"，所以条件要和 run 一致。
      若 headed 有登录态而 headless 没有，那正是"run 会看到登录页"的原因 ——
      这时报失败是对的，而且下面的输出会把这个差别指出来。
    """
    print(f"③ 用同一个 profile **新开一个会话**复核（headless={HEADLESS}，无人工操作）", flush=True)
    session = await _open_with_retry(profile, headless=HEADLESS)
    try:
        await session.navigate_to(url)
        # 给页面一点时间完成跳转（这里是一次性等待，不是轮询）。
        await asyncio.sleep(3.0)
        final_url, dom_text = await read_page_state(session)
        verdict, why = classify(final_url, dom_text)
        return verdict == LOGGED_IN, why, final_url
    finally:
        await close_gracefully_and_flush(session)


async def run(args: argparse.Namespace) -> int:
    profile = Path(args.profile).expanduser().resolve()
    print(f"profile 目录：{profile}")

    if os.getenv("CI"):
        # 这个脚本要开一个有窗口的浏览器、还要人扫码。CI 上它没有任何意义，
        # 而"在 CI 上跑起来"最坏的结果是有人以为登录是自动的。
        print("CI 环境里不跑登录脚本（它需要人扫码）。", file=sys.stderr)
        return 2

    try:
        existing = read_mark(profile)
    except ProfileMarkError as exc:
        existing = None
        print(f"⚠️  已有标记但读不出来：{exc}")
    if existing is not None:
        print(f"⚠️  这个 profile 里已经有一次登录记录：{existing.describe()}")
        print("    本次会覆盖它（重新登录会把上一次的现场盖掉，这是有意的）。")

    profile.mkdir(parents=True, exist_ok=True)

    ok, why = await phase_a_human_login(profile, args.url, args.timeout_s)
    if not ok:
        print()
        print("❌ 没等到登录态。这次什么都没写 —— profile 里不会留下假的成功标记。")
        print(f"   依据：{why}")
        print("   常见原因：没扫完就超时（可 --timeout-s 调大）、扫完但停在二次验证页、")
        print("             网络到不了 mms.pinduoduo.com。看一眼上面的截图最省事。")
        return 1

    passed, verify_why, final_url = await phase_b_verify_fresh(profile, args.url)
    if not passed:
        print()
        print("❌ 新会话复核**没通过**：cookie 没留下来（或没生效）。")
        print(f"   依据：{verify_why}")
        print("   ★ 这是本脚本最想抓到的情况：如果不复核，你会以为登录成功了，")
        print("     然后每一次 run 都停在登录页、报'需要人工登录'、零行数据。")
        return 1

    verified_at = _now_iso()
    target = write_mark(
        profile, site=SITE, logged_in_url=final_url, verified_at=verified_at
    )
    print()
    print("✅ 登录态已确认可用（新会话复核通过）")
    print(f"   依据：{verify_why}")
    print(f"   标记：{target}")
    print()
    print("── 接下来这么跑 ──")
    if args.profile != USER_DATA_DIR:
        print("★ 先把这一行加进 .env（.env 已在 .gitignore 里）：")
        print(f"    ECOM_AGENT_USER_DATA_DIR={profile}")
        print("  只有两边指向**同一个**目录，登录态才会被用上。")
        print("  （不加也行，但每次 run 都得手写 --profile，而写错的那次不会报错，")
        print("    只会以'需要人工登录'平静收场。）")
    print("  然后：")
    print(f"    uv run python main.py run tasks/pdd_shop_overview.yaml --profile \"{profile}\"")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="login_pdd",
        description="人工扫码登录拼多多商家后台，把登录态留在持久 profile 里",
    )
    p.add_argument(
        "--profile",
        default=USER_DATA_DIR or str(DEFAULT_PROFILE_DIR),
        help="持久 profile 目录。默认取 ECOM_AGENT_USER_DATA_DIR；"
             f"没配则用 {DEFAULT_PROFILE_DIR}（并会在最后提示你把它写进 .env）",
    )
    p.add_argument("--url", default=PDD_HOME, help=f"登录后要回到的页面（默认 {PDD_HOME}）")
    p.add_argument(
        "--timeout-s", type=float, default=300.0,
        help="等扫码的上限秒数（默认 300）",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 级日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # 库自己的日志压到 WARNING：它在 INFO 级会把每一步的 DOM 全文打出来，
    # 把我们要看的那几行人话淹没掉（登录脚本的输出是给人读的，不是给日志读的）。
    logging.getLogger("browser_use").setLevel(logging.WARNING)
    logging.getLogger("BrowserSession").setLevel(logging.WARNING)
    logging.getLogger("utils").setLevel(logging.WARNING)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
