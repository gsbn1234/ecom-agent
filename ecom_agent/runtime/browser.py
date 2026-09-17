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
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from ecom_agent.dsl.compiler import CompiledTask

logger = logging.getLogger(__name__)


def build_browser_session(compiled: CompiledTask) -> Any:
    """按编译产物造一个 BrowserSession。★ 不做任何多余的事。"""
    from browser_use import BrowserSession

    kwargs = dict(compiled.browser_kwargs)
    session = BrowserSession(**kwargs)
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
