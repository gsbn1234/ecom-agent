"""pytest 全局夹具。

★ 这里做两件事：
  1. 把项目根插进 sys.path（下面第一段）。
  2. 可选的诊断钩子：把 browser-use 丢掉的 Chrome 输出截下来（第二段）。

★ 为什么第 1 条要有：pytest 默认只把「用例所在目录」加进 sys.path（rootdir 模式），
  不认 ecom_agent 这个包。没有这行，`from ecom_agent.dsl.loader import load` 会
  ModuleNotFoundError，而且报错信息会误导你去查包结构，而不是查 sys.path。
  注：装成 editable（pip install -e .）之后其实不需要这行，但那样测试就依赖
  "环境装对了"这个前提，clone 下来直接 pytest 会失败。成本两行，收益是不依赖安装状态。
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── 诊断钩子：把 browser-use 丢掉的 Chrome 输出截下来 ──────────
# ★ 为什么需要它（一次真实的排查，不是假想的）：
#   CI 上 needs_browser 全红，而公开注解只给到一句
#   local_browser_watchdog.py:428 的 RuntimeError。去读库源码才知道这句消息
#   是【硬编码】的通用提示，而 Chrome 是用 stderr=PIPE 起的
#   （local_browser_watchdog.py:146-151）—— 全库【没有任何一行读那个管道】。
#   于是"Chrome 为什么退出"这个唯一重要的信息，被接了管道却没人听，直接丢了。
#
# ★ 手法：不去改库，也不去 patch 它的函数体 —— 在 create_subprocess_exec 这一层
#   把 PIPE 换成【文件句柄】。于是"没人读管道"这件事不再重要：输出直接落盘。
#   选这一层还有两个好处：对所有起子进程的地方一次生效（不用去数库里有几个启动点），
#   以及顺手记下库真正用的 argv（那是"探针起得来、库起不来"唯一能直接对比的东西）。
#
# ★ 为什么用环境变量开关、默认关闭：
#   这个钩子会替换 asyncio 的全局函数，属于"测试进程里的环境改造"。
#   默认开启等于让所有本地跑测试的人都活在一个被改过的 asyncio 上 ——
#   而它恰恰是用来诊断"环境和别人不一样"的工具，自己制造环境差异就南辕北辙了。
#   CI 里由 env 打开（见 .github/workflows/ci.yml 的 browser job）。
@pytest.fixture(scope="session", autouse=True)
def _capture_child_process_stdio():
    log_path = os.getenv("ECOM_AGENT_CAPTURE_CHROME_LOG", "").strip()
    if not log_path:
        yield
        return

    real_exec = asyncio.create_subprocess_exec
    fh = open(log_path, "ab")  # noqa: SIM115  活到 session 结束

    def _log(line: str) -> None:
        fh.write((line + "\n").encode("utf-8", "replace"))
        fh.flush()  # ★ 必须立刻 flush：测试进程可能是被 kill 掉的，缓冲会连带证据一起丢

    async def spy(*argv, **kw):
        _log("")
        _log("=" * 70)
        _log(f">>> 第 {spy.n} 次启动子进程 @ {time.strftime('%H:%M:%S')}")  # type: ignore[attr-defined]
        spy.n += 1  # type: ignore[attr-defined]
        _log(f">>> 二进制: {argv[0] if argv else '(无)'}")
        _log(f">>> 参数({max(0, len(argv) - 1)}): {' '.join(str(a) for a in argv[1:])}")
        # ★ 只换 PIPE，别的（None / 继承 / 已有 fd）一律不动 ——
        #   把 None 也换成文件会让"本该继承父进程 stdio"的调用静默改变行为。
        for key in ("stdout", "stderr"):
            if kw.get(key) == asyncio.subprocess.PIPE:
                kw[key] = fh
        proc = await real_exec(*argv, **kw)
        _log(f">>> PID {proc.pid}")
        return proc

    spy.n = 0  # type: ignore[attr-defined]
    asyncio.create_subprocess_exec = spy  # type: ignore[assignment]
    try:
        yield
    finally:
        asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]
        fh.close()

