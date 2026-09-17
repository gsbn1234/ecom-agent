"""浏览器会话：构造、预热导航、以及【保证关闭】。

★★ 本文件存在的核心理由是一个会泄漏进程的坑：

  `compiler` 里把 `keep_alive=True` 定成了硬要求（理由见 compiler.py 里那段：
  任何"停机再续跑"的方案都必须在第二次 run 时还能拿到活着的浏览器）。
  但 keep_alive 的另一面是 **`Agent.close()` 就不会 kill 浏览器了**：

      service.py:3981-3988
          if not self.browser_session.browser_profile.keep_alive:
              await self.browser_session.kill()
          else:
              await self.browser_session.event_bus.stop(...)   # ← 只停事件总线

  也就是说 `run()` 返回之后，Chrome 进程**还活着**，而且没有任何人负责收尾。
  本项目每跑一个 run 启一个会话 → 不显式 kill 就是**一次运行泄漏一个 Chrome**。
  这种泄漏在开发机上表现为"跑了几十次之后机器变卡"，而没人会把它和
  keep_alive 联系起来。所以关闭必须是**结构化**的（context manager），
  而不是"记得在成功路径上调一下"。

★ 顺带一个优雅的巧合：`kill()` 内部会 `reset()`，而 `reset()` 正是那个
  **原地 `.clear()` selector_map** 的操作（S2-4）。也就是说，本文件的关闭动作
  恰恰是"如果按常规写法存了 browser_state 对象就会毁掉全部记录"的那一下。
  我们能安全地在最后 kill，唯一的原因是记录器在回调里就把值取走了。
  —— 关闭顺序本身就是那条纪律的验收。

★★ 第二个坑（Phase 6 实测出来的，比上面那个更阴）：详见 `pin_user_data_dir`。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from ecom_agent.dsl.compiler import CompiledTask

logger = logging.getLogger(__name__)


def pin_user_data_dir(session: Any, profile_dir: str | Path) -> Path:
    """把会话实际使用的 user_data_dir **钉回**我们给的目录。返回值是钉住的那个路径。

    ★★ 为什么必须钉（2026-09-17 实测，不是读文档推断的）：

      `BrowserProfile.model_post_init` 会调 `_copy_profile()`。只要 user_data_dir
      非空、且它判定"这是 Chrome"，就把整个目录**拷到一个临时目录**再启动，
      并把 `self.user_data_dir` 换成那个临时目录：

          INFO [utils] Created new profile (Default) in temp directory:
                      C:\\Users\\...\\Temp\\browser-use-user-data-dir-xxxx

      这行 INFO 读起来人畜无害，实际含义是"**你给的那个目录根本没被用**"。

      对我们的后果是致命的：**登录态写不回原目录**。人工扫码一次、脚本报成功、
      cookie 落在 %TEMP% 里随进程消失 → 下一次 run 看到登录页 → 按任务文本
      "遇登录页立即停止并汇报需要人工登录"收场 → 退出码 0、报告齐全、**零行数据**。
      一条完全静默的失败，而且人会先去怀疑风控和 cookie 过期。

    ★ 为什么不是"把目录名换成不含 chrome 的"：`is_chrome` 同时也看
      executable_path，而我们永远传 chrome.exe —— 换个名字照样命中。
    ★ 为什么不是"给目录起名 browser-use-user-data-dir-xxx"去命中库的豁免分支：
      那是在**冒充库自己的临时目录**。库哪天加一句"清理自己的临时目录"，
      我们的登录态就被删了 —— 借来的豁免迟早要还。
    ★ 为什么钉在 `session.browser_profile` 上，而不是我们自己造一个 profile 传进去：
      实测 `BrowserSession(browser_profile=我们造的)` **不使用**那个对象
      （`is` 判定为 False），它自己再建一份，于是 _copy_profile 又跑一次。
      钉在外部对象上等于没钉 —— 这个坑我先踩了一次才改对。
    ★ 为什么是"构造之后赋值"而不是"构造前传参"：_copy_profile 只在
      `model_post_init` 里跑一次（构造时），之后的赋值不会再触发它；
      而启动参数是 `start()` 时才通过 `get_args()` 读的 —— 所以赋值来得及生效。
      ⚠️ 这个时序是本函数成立的前提，`tests/test_profile_persistence.py` 用
      真浏览器把它钉住了（含对照实验：不钉 → 登录态丢失）。

    ⚠️ 已知代价，写在这里不藏：钉住之后，**同一个 profile 目录不能被两个会话同时用**
      （Chrome 的 profile 独占锁）。以前库总是拷到各自的临时目录，反而"顺带"避开了
      这个冲突。本项目 run 是串行的、登录脚本也不该和 run 并行，所以可以接受；
      但"两个 run 同时用同一个 profile"会起不来 —— 那时的报错是 Chrome 的
      profile 锁，跟"登录态"毫无关系，别再往风控上想。
    """
    target = Path(profile_dir)
    profile = session.browser_profile
    redirect = Path(profile.user_data_dir) if profile.user_data_dir else None
    profile.user_data_dir = target

    if redirect is not None and redirect != target:
        # ★ 不静默：这正是"如果不钉会怎样"的证据，值得每次都说一句（DEBUG 级，
        #   因为它对每次运行都成立、且我们已经处理了）。
        logger.debug(
            "库把 user_data_dir 重定向到了 %s（_copy_profile 的默认行为）；已钉回 %s",
            redirect,
            target,
        )
    logger.info("持久 profile：%s", target)
    return target


def build_browser_session(compiled: CompiledTask) -> Any:
    """按编译产物造一个 BrowserSession。★ 不做任何多余的事。"""
    from browser_use import BrowserSession

    kwargs = dict(compiled.browser_kwargs)
    session = BrowserSession(**kwargs)
    if kwargs.get("user_data_dir"):
        pin_user_data_dir(session, kwargs["user_data_dir"])
    logger.debug(
        "BrowserSession 已构造：allowed=%s prohibited=%s headless=%s executable=%s",
        kwargs.get("allowed_domains"),
        kwargs.get("prohibited_domains"),
        kwargs.get("headless"),
        kwargs.get("executable_path") or "(库自己探测)",
    )
    return session


@asynccontextmanager
async def browser_session(compiled: CompiledTask, *, warmup_url: str = "") -> AsyncIterator[Any]:
    """打开一个会话，**无论怎么退出都关掉它**。

    ★ 为什么在这里 `start()`，而不是交给 `run()`（它自己也会 start，见 service.py:2563）：
      启动失败（Chrome 路径不对 / 沙箱不可用 / playwright 没装）应该在**烧掉任何
      token 之前**爆出来。交给 run() 的话，报错发生在 Agent 已经构造、任务文本
      已经拼好、第一条消息可能已经发出去之后 —— 而那时候你没法确定
      这次失败到底花了多少 token。

      `start()` 是幂等的（`on_BrowserStartEvent` 的 docstring 明写
      "calling start() multiple times is safe"），所以之后再 start 一次没有副作用。

    ★ `warmup_url` 由 runner 传进来，且**必须已经过白名单校验**（见 runner 的
      `_check_start_url`）。这里不重复校验 —— 那会让"校验在哪一层"变得含糊，
      而含糊的安全校验比没有校验更危险（你以为它在验）。
    """
    session = build_browser_session(compiled)
    started = False
    try:
        await session.start()
        started = True

        if warmup_url:
            # ★ 走 `navigate_to` 而不是自己拿 page.goto：它派发 NavigateToUrlEvent，
            #   于是**库自己的 Layer 0（SecurityWatchdog）会在这条路上生效**。
            #   绕过它等于让"白名单"只在我们自己写的那段校验里成立 ——
            #   而 Layer 0 的价值恰恰是"我们写漏了的时候它还挡着"。
            await session.navigate_to(warmup_url)
            logger.info("已导航到任务起点：%s", warmup_url)

        yield session
    finally:
        await kill_quietly(session, started=started)


async def kill_quietly(session: Any, *, started: bool = True) -> None:
    """关掉会话，失败只记日志不抛。

    ★ 为什么吞异常：这个函数在 `finally` 里跑。让它抛的话，
      一个"关不掉浏览器"的次要问题会**顶掉**正在传播的真正错误
      （比如 LLM 报错、护栏硬停），排查时看到的就变成了关闭失败。
      但也不能静默 —— 关不掉就是进程泄漏，必须留痕。
    """
    if not started:
        # 从没 start 过的会话：kill() 会派发停止事件并 reset，
        # 对一个没有 event_bus 处理器的会话来说是一串无意义的动作。
        # 直接关掉底层连接即可。
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭未启动的会话时出错（可忽略）：%s", exc)
        return

    try:
        await session.kill()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "浏览器会话 kill() 失败：%s: %s —— 可能残留一个 Chrome 进程，"
            "用任务管理器/ps 确认一下",
            type(exc).__name__,
            exc,
        )


async def close_gracefully_and_flush(session: Any, *, settle_s: float = 2.0) -> bool:
    """优雅关掉 Chrome，让 **cookie 真的落盘**。返回是否走成了优雅路径。

    ★★ 为什么登录流程不能用 `kill()` 收尾（2026-09-17 离线实测，三组对照）：

      `kill()` 是强杀。而 **Chrome 的 cookie 库不是写一次落一次盘** ——
      它按定时器批量提交（实测：设完 cookie 等 35 秒后库里就有行，
      只等 3 秒则**零行**，而同一时刻 `document.cookie` 明明读得到）。
      强杀时那些"还在内存里"的 cookie 直接消失。

      后果同 pin_user_data_dir 那段：扫码成功 → 脚本报成功 → cookie 没了 →
      下一次 run 看到登录页 → **静默零行**。

    ★ 三条路的实测结果（都是真浏览器 + 本地 http.server，零 token）：
        · 设完 cookie 等 35 秒再 kill   → 库里 1 行，新会话读得回来 ✔（但凭什么让用户等 35 秒）
        · 走库的 `session.stop()` 再 kill → 库里 **0 行**，新会话读不到 ✘（stop 不等于优雅退出）
        · **CDP 的 `Browser.close()`**    → 立刻优雅退出、库里 1 行，新会话读得回来 ✔ ← 用这条

      `Browser.close` 是标准 CDP 命令（`cdp_client.send.Browser.close()`），
      不是库的私有 API，所以它比"等定时器"和"猜 stop 的语义"都稳。

    ⚠️ 关掉之后库的 StorageStateWatchdog 会连着报一串
      `ConnectionError: Reconnection failed — CDP still not connected` 的 traceback
      —— 那是它自己的后台任务发现浏览器没了。**与登录态无关，也不影响结果**，
      所以这里只记一行 DEBUG，不当失败。
    """
    try:
        await session.cdp_client.send.Browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "优雅关闭（CDP Browser.close）失败：%s: %s —— 退回强杀。"
            "★ 这时 cookie 可能没落盘，请以随后的【新会话复核】结果为准",
            type(exc).__name__,
            exc,
        )
        await kill_quietly(session)
        return False

    # 给 Chrome 一点时间完成退出前的落盘动作（这一步是"等"而不是"猜"：
    # 等不到也没关系 —— 复核步骤会当场揭穿）。
    await asyncio.sleep(settle_s)
    logger.debug("已用 Browser.close 优雅关闭浏览器（cookie 应已落盘）")
    await kill_quietly(session)
    return True
