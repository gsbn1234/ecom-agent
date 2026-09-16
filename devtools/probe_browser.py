"""探针：把「浏览器起不来」从一句通用 RuntimeError 变成一条可读的原因。

★★ 为什么需要这个文件（一次真实排查的完整教训，不是假想的）：

   背景：CI 上 needs_browser 全红，公开注解只给到一句

       local_browser_watchdog.py:428 in _wait_for_cdp_url
         raise RuntimeError(
       ...<4 lines>...
       )
       RuntimeError:

   第一版探针只回答了「Chrome 在不在」——答案是在（/usr/bin/google-chrome），
   然后测试照样红。读库源码后确认，「把 traceback 打全」这条路的每一环都是死的：

     1. 第 428 行的 RuntimeError 消息是【硬编码】的通用提示
        （"Browser process (PID n) exited before CDP became available ... e.g.
         --no-sandbox may be needed ... or a virtual display such as Xvfb"）。
        把 --tb=short 改成 --tb=long 只是把一句读过源码就知道的话打全。
        ⚠️ 而且这个改法本身就是错的：输出里的 "...<4 lines>..." 不是 pytest 压的
           （--tb long 改完输出逐字未变），是【Python 3.11+ traceback 模块自己】
           对多行 raise 语句的截断，没有任何 pytest 开关能关掉它。
     2. 第 146-151 行用 stderr=asyncio.subprocess.PIPE 起 Chrome，
        而全库【没有任何一行读这个管道】（第 377 行的 stderr 属于
        _install_browser_with_playwright，另一个函数）。
        → Chrome 临死前喊的那句话，被接了管道却没人听，直接丢了。

   ★ 而且环境本身已经被证伪了：探针第一段用【同样的二进制、同样的 get_args()、
     同样的 headless】自己起了一次，CDP 正常就绪（注解原文：
     "探针：按库的参数能起 Chromium 且 CDP 就绪 → 环境没问题，去查我们自己代码"）。
     所以「缺库 / 沙箱 / 没有 Xvfb」这些 R9 式的解释全部排除 ——
     同一个 runner 上，我起得来，库起不来，差别在【启动方式】里。

   于是本文件分成两段，第二段是真正的诊断手段：既然库不读那个管道，
   那就【别用管道】—— 劫持 create_subprocess_exec，把 PIPE 换成文件句柄，
   再跑库真正的启动路径（BrowserSession.start()）。这样即使库一行都不读，
   Chrome 的输出也已经落盘了。

★ 为什么用库自己的 get_args() 而不是自己拼一份参数：
  自己拼的参数即使能让 Chrome 起来，证明的也只是"我拼的那份能起来"。
  要回答的问题是"browser-use 起不来，为什么"，那就必须用它真正会用的那份参数。

★ 为什么失败也 exit 0：
   它是探针，职责是【说明】，不是【判决】。红了由下游的 pytest 去红。
   探针自己变红只会制造第二个需要解释的红色，而"需要解释的红色"正是这次排查里
   最费时间的东西。
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 等 CDP 的时间。库里的默认是 30s（_wait_for_cdp_url 的 timeout 参数）。
# 这里给 15s：探针要的是"能不能起来"，健康的 headless Chrome 通常 1-3s 就绪；
# 等满 30s 只会让探针本身变成 CI 里最慢的一步。
CDP_TIMEOUT_S = 15.0
# 第二段跑库真正启动路径的总超时。库自己的 CDP 等待是 30s，
# 留够余量以免我们把库的超时伪装成自己的超时。
LIBRARY_TIMEOUT_S = 90.0


def _ann(kind: str, msg: str) -> None:
    """打印一行注解，并回显到 stdout。

    ★ 为什么两种都做：::notice:: 是给 CI 页面看的（公开可读，见 ci.yml 里的说明），
      stdout 是给人直接跑这个脚本时看的。只做前者，本地跑就没输出；
      只做后者，CI 上就白跑。一行成本，两个场景都成立。
    """
    print(f"[{kind}] {msg}")
    print(f"::{kind}::{msg}")


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome_version(path: str) -> str:
    try:
        p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        return (p.stdout or p.stderr).strip() or f"(退出码 {p.returncode}，无输出)"
    except Exception as e:  # noqa: BLE001
        return f"(--version 都跑不起来: {type(e).__name__}: {e})"


def _missing_libs(path: str) -> list[str]:
    """Linux 上查缺失的共享库。

    ★ 这是"进程起来后立刻退出"最经典的原因，而且 ldd 一秒就能给出答案 ——
      没有理由先去猜 sandbox。Chrome 的 stderr 会说
      "error while loading shared libraries: libX.so"，但那是二手的；
      ldd 直接把整张缺失清单列全（stderr 只报第一个，修完一个还有下一个）。
    """
    if not shutil.which("ldd"):
        return []
    try:
        p = subprocess.run(["ldd", path], capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if "not found" in ln]


# ── 第一段：我按库的参数起一次（证伪"环境问题"）────────────
async def _try_launch(browser_path: str, args: list[str], port: int) -> tuple[str, str, int | None]:
    """按库的参数起一次 Chrome，读它的 stderr。

    返回 (结论, stderr, 退出码)。结论 ∈ {"cdp_ok", "exited", "timeout"}。
    """
    proc = await asyncio.create_subprocess_exec(
        browser_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        # ★ 和库第 150 行完全一样是 PIPE —— 区别只在于【我们真的读它】。
        stderr=asyncio.subprocess.PIPE,
    )
    print(f"  进程 PID = {proc.pid}")

    deadline = asyncio.get_running_loop().time() + CDP_TIMEOUT_S
    verdict = "timeout"
    while asyncio.get_running_loop().time() < deadline:
        # 先判死再看 CDP：进程已经没了的话，再等只是白等满 15 秒。
        if proc.returncode is not None:
            verdict = "exited"
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    body = json.loads(r.read().decode("utf-8", "replace"))
                    print(f"  CDP 就绪: {body.get('Browser', '?')}")
                    verdict = "cdp_ok"
                    break
        except (urllib.error.URLError, OSError, ValueError):
            pass  # 还没起来，正常
        await asyncio.sleep(0.2)

    if verdict == "cdp_ok":
        proc.terminate()
    elif proc.returncode is None:
        proc.kill()

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        stderr = b""
    return verdict, stderr.decode("utf-8", "replace").strip(), proc.returncode


# ── 第二段：跑【库自己的】启动路径，但把它的管道偷换成文件 ────
async def _library_launch(session_kw: dict) -> dict:
    """跑 BrowserSession.start()，把库丢掉的 Chrome 输出截下来。

    ★ 核心手法：库用 stderr=PIPE 起 Chrome 却从不读那个管道。
      我们不跟它讲道理，也不去 patch 它的函数体 —— 直接在
      create_subprocess_exec 这一层把 PIPE 换成【文件句柄】。
      于是"没人读管道"这件事不再重要：输出直接落盘。

    ★ 为什么在 create_subprocess_exec 这一层动手，而不是改 local_browser_watchdog：
      1. 库源码只读不改是本项目的红线；
      2. 这一层的拦截对【所有】起子进程的地方都成立，不用去数库里有几个启动点；
      3. 它顺手把库真正用的 argv 也截下来了 —— 那是"我的第一段和库到底差在哪"
         唯一能直接对比的东西。
    """
    from browser_use import BrowserSession

    real_exec = asyncio.create_subprocess_exec
    captured: dict = {"argv": None, "log": None, "proc": None}
    tmpdir = tempfile.mkdtemp(prefix="probe-lib-")
    log_path = Path(tmpdir) / "chrome.log"
    fh = open(log_path, "wb")  # noqa: SIM115  活到 finally 才关

    async def spy(*argv, **kw):
        captured["argv"] = list(argv)
        # ★ 只换 PIPE，别的（None / 继承 / 已有 fd）一律不动 ——
        #   把 None 也换成文件会让"本该继承父进程 stdio"的调用静默改变行为。
        for key in ("stdout", "stderr"):
            if kw.get(key) == asyncio.subprocess.PIPE:
                kw[key] = fh
        proc = await real_exec(*argv, **kw)
        captured["proc"] = proc
        return proc

    asyncio.create_subprocess_exec = spy  # type: ignore[assignment]
    session = None
    try:
        session = BrowserSession(headless=True, **session_kw)
        await asyncio.wait_for(session.start(), timeout=LIBRARY_TIMEOUT_S)
        captured["ok"] = True
        captured["error"] = None
    except Exception as e:  # noqa: BLE001
        captured["ok"] = False
        # ★ 用 repr 而不是 str：我们要看的就是那条消息本身。Python 对多行 raise
        #   会把源码显示截成 "...<N lines>..."（这是 traceback 模块干的，与 pytest 无关），
        #   但异常对象上的消息是完整的 —— 直接取它，绕开所有显示层的截断。
        captured["error"] = f"{type(e).__name__}: {e}"
    finally:
        asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]
        if session is not None:
            try:
                await asyncio.wait_for(session.kill(), timeout=30)
            except Exception:  # noqa: BLE001
                pass
        fh.flush()
        fh.close()

    raw = log_path.read_text(encoding="utf-8", errors="replace").strip()
    proc = captured.get("proc")
    captured["log_text"] = raw
    captured["returncode"] = getattr(proc, "returncode", None)
    return captured


def main() -> int:
    print("=" * 70)
    print("浏览器探针")
    print("=" * 70)
    print(f"平台      : {platform.platform()}")
    print(f"python    : {platform.python_version()}")
    print(f"CI        : {os.getenv('GITHUB_ACTIONS', '(未设置，本地)')}")

    # ── 0. 二进制找得到吗 ────────────────────────────────
    from browser_use.browser.chrome import find_chrome_executable

    from ecom_agent.config import CHROME_PATH

    # ★ 必须按【项目自己的解析顺序】来，不能直接调 find_chrome_executable()。
    #   项目走的是 config.CHROME_PATH → BrowserSession(executable_path=...)（见 tests/stubs/site.py 的
    #   make_session），只有该值为空串时才回退到 find_chrome_executable()。
    #   CI 上 ECOM_AGENT_CHROME_PATH 恰好是空串，两种写法结果一样 ——
    #   但在开发机上 CHROME_PATH 指向 playwright 的 Chrome，直接调 find_chrome_executable()
    #   会返回 None，于是探针在【本地】大喊"找不到 Chrome"而测试跑得好好的。
    #   一个本地会撒谎的探针，就没人会在本地跑它；而没人在本地跑的探针，
    #   等于把排查又推回 CI 那一轮 5 分钟的往返。
    probed = find_chrome_executable()
    browser_path = CHROME_PATH or probed
    print(f"\nconfig.CHROME_PATH          -> {CHROME_PATH or '(空，回退到自动探测)'}")
    print(f"find_chrome_executable()    -> {probed}")
    print(f"实际使用                    -> {browser_path}")
    if not browser_path:
        _ann("warning", "browser-use 找不到 Chrome —— 下游浏览器测试的红是【环境问题】，不是代码问题")
        return 0
    print(f"版本      : {_chrome_version(browser_path)}")
    _ann("notice", f"browser-use 探测到的 Chrome: {browser_path}")

    # ── 1. 缺共享库吗（Linux）────────────────────────────
    missing = _missing_libs(browser_path)
    if missing:
        _ann("warning", "缺失共享库（这是 Chrome 启动即退出的头号原因）: " + "; ".join(missing))
    else:
        print("ldd       : 无 'not found'（或非 Linux，跳过）")

    # ── 2. 第一段：我按库的参数起一次（证伪"环境问题"）────
    from browser_use import BrowserSession

    headless = os.getenv("ECOM_AGENT_HEADLESS", "true").strip().lower() in ("1", "true", "yes")
    # ★ executable_path 只在"项目自己指定了"时才传 —— 与 make_session 一致。
    #   它不是 get_args() 的一部分（库单独用这个字段挑二进制，见 local_browser_watchdog.py:124-135），
    #   所以对参数列表没影响，但传不传决定了这个 session 是否真的等价于测试里的那个。
    session_kw = {"executable_path": CHROME_PATH} if CHROME_PATH else {}
    args = BrowserSession(headless=headless, **session_kw).browser_profile.get_args()
    port = _port()
    args.append(f"--remote-debugging-port={port}")

    print("\n" + "─" * 70)
    print("第一段：我按库的参数自己起（用来证伪『环境坏了』）")
    print("─" * 70)
    print(f"headless  : {headless}")
    print(f"参数个数  : {len(args)}（来自 BrowserSession.browser_profile.get_args()，与库同源）")
    print(f"CDP 端口  : {port}")

    # ★ 单独把扩展目录拎出来看一眼：CI 上这些路径来自 $HOME，
    #   如果不存在，Chrome 会打 "Failed to load extension"。通常不致命，
    #   但它是"参数看着一样、行为不一样"的一个真实来源，值得单独报一行。
    exts: list[str] = []
    for a in args:
        if a.startswith("--load-extension="):
            exts.extend(a.split("=", 1)[1].split(","))
    if exts:
        print("扩展目录  :")
        for e in exts:
            print(f"    {'存在' if Path(e).is_dir() else '★不存在'} {e}")

    verdict, stderr, code = asyncio.run(_try_launch(browser_path, args, port))
    print(f"\n启动结论  : {verdict}   退出码: {code}")
    print(f"Chrome 的 stderr: {stderr or '（空）'}")

    if verdict == "cdp_ok":
        _ann("notice", "第一段：按库的参数能起 Chromium 且 CDP 就绪 → 环境没问题，差别在库的启动方式")

    # ── 3. 第二段：跑库自己的启动路径，把输出截下来 ────────
    print("\n" + "─" * 70)
    print("第二段：跑库自己的 BrowserSession.start()，但把它的管道偷换成文件")
    print("（库把 Chrome 的 stderr 接进 PIPE 却从不读 —— 那就别用管道）")
    print("─" * 70)
    lib = asyncio.run(_library_launch(session_kw))

    if lib.get("argv"):
        argv = lib["argv"]
        print(f"库真正用的二进制 : {argv[0]}")
        print(f"库真正用的参数   : {len(argv) - 1} 个")
        extra = [a for a in argv[1:] if a.startswith("--remote-debugging-port")]
        print(f"  其中 CDP 端口  : {extra or '(无)'}")
        # ★ 参数对比才是"我起得来、库起不来"的落点。只报差异，不报整份
        #   （60 个参数全打会把注解撑爆，而注解是有长度限制的公共通道）。
        mine = set(args) - {f"--remote-debugging-port={port}"}
        theirs = set(argv[1:]) - set(extra)
        only_lib, only_mine = sorted(theirs - mine), sorted(mine - theirs)
        print(f"  仅库有         : {only_lib or '(无)'}")
        print(f"  仅我有         : {only_mine or '(无)'}")
    else:
        print("库没走到起子进程那一步（可能在此之前就失败了）")

    print(f"\n库的启动结果 : {'成功' if lib.get('ok') else '失败'}")
    print(f"库的异常     : {lib.get('error') or '(无)'}")
    print(f"Chrome 退出码: {lib.get('returncode')}")

    log_text = lib.get("log_text") or ""
    print("-" * 70)
    if log_text:
        print("被库丢掉的 Chrome 输出（这就是死因该在的地方）:")
        print("-" * 70)
        print(log_text)
    else:
        print("Chrome 输出: （空 —— 一个字都没写出来，说明它连初始化都没走完）")
    print("-" * 70)

    # ── 4. 结论 ─────────────────────────────────────────
    if lib.get("ok"):
        _ann(
            "notice",
            "第二段：库自己的启动路径也成功了 → 说明失败与启动方式无关，"
            "去查测试里的那份配置（conftest/夹具和本探针的差别）",
        )
    else:
        tail = " | ".join(log_text.splitlines()[-5:]) if log_text else "(Chrome 一个字都没输出)"
        _ann(
            "error",
            f"第二段：库自己的启动失败。异常={lib.get('error') or '?'}；"
            f"退出码={lib.get('returncode')}；Chrome 输出末尾：{tail}",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
