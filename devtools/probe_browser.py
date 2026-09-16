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
    # ★ 注解要按 GitHub 的规则转义，否则消息里一个裸露的 % 就会让解析错位。
    #   顺序不能反：必须【先】把 % 转成 %25，再转换行 ——
    #   反过来的话，%0A 里的那个 % 会被第二次转义成 %250A，消息就坏了。
    #   这条纪律原来写在 workflow 的 shell 里（sed -e 's/%/%25/g'），
    #   把那段换成调用本脚本时差点丢掉 —— 所以它属于这里，属于唯一
    #   生成注解行的地方，而不是每一处调用点。
    esc = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{kind}::{esc}")


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
async def _try_launch(
    browser_path: str, args: list[str], port: int, timeout: float = CDP_TIMEOUT_S
) -> tuple[str, str, int | None]:
    """按给定参数起一次 Chrome，读它的 stderr。

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

    deadline = asyncio.get_running_loop().time() + timeout
    verdict = "timeout"
    while asyncio.get_running_loop().time() < deadline:
        # 先判死再看 CDP：进程已经没了的话，再等只是白等满超时。
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


# ── 从 Chrome 输出里挑出"为什么死"的那几行 ──────────────────
#
# ★ 为什么不能用 tail：实测踩过一次 —— Chrome 崩溃时 stderr 的末尾几十行
#   全是栈帧（#0..#19 加一整排寄存器），真正的死因（那行 FATAL / Check failed）
#   在栈【前面】，正好被 tail 挤出去。
#   tail 对"人写的日志"好用，对"崩溃转储"恰恰相反：越靠后越没有信息量。

# 一档：死亡关键词。命中就一定是死因，这类行【从来不会】出现在健康运行里。
# ★ 这是白名单而不是黑名单，方向很重要 —— 见 fatal_lines 的 docstring。
DEATH_MARKERS = ("FATAL", "Check failed", "Received signal", "zygote", "sandbox")

# 二档：已知的良性噪声。只在【一条死亡关键词都没命中】时才用来兜底。
#   形态必须是"一类噪声"，不能是"某次看到的那个字符串"：
#   · cpufreq —— crashpad 读不到的 sysfs 文件
#   · dbus/   —— 容器里没有 D-Bus。★ 匹配的是目录前缀而不是具体文件：
#               第一版写死 "dbus/bus.cc"，下一次运行 Chrome 改从
#               dbus/object_proxy.cc 报同样的错，白名单没命中，红字原样回来。
KNOWN_NOISE = ("cpufreq", "dbus/")


def fatal_lines(text: str) -> list[str]:
    """从 Chrome 输出里挑出"为什么死"的那几行。返回值的语义见下。

    ★★ 两档制，而不是"黑名单过滤 ERROR"。这个设计是被 bug 逼出来的：

      第一版是黑名单 —— "所有 ERROR 行，除了噪声"。它有个致命的不对称：
      **它保证会误报**。只要 runner 上还会冒出任何一种我没想到的良性 ERROR，
      注解里就会出现一条没有信息量的红字，标题还写着「死因行」。
      实测发生了两次（dbus/bus.cc 修完，下次变成 dbus/object_proxy.cc）。

      而黑名单想换来的那个好处 —— "不放过未知故障" —— 用白名单加一档兜底
      就能拿到：死亡关键词一条都没命中时，才退回去报那些非噪声的 ERROR。
      那时它们是唯一线索，报出来是对的；而只要有真死因，它们就不会出现。

      换句话说：**有真凶时只报真凶，没有真凶时才报嫌疑人。**
      一份"总在喊狼来了"的注解，等于没有注解 —— 这是本项目反复吃到的同一个教训。
    """
    lines = [ln.strip() for ln in text.splitlines()]
    seen: set[str] = set()

    def _dedup(items) -> list[str]:
        out = []
        for s in items:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    primary = _dedup(s for s in lines if any(k in s for k in DEATH_MARKERS))
    if primary:
        return primary
    # 兜底档：没有死亡关键词。此时非噪声的 ERROR 是我们仅有的线索。
    return _dedup(
        s for s in lines if "ERROR:" in s and not any(n in s for n in KNOWN_NOISE)
    )


# ★ 关沙箱用的三个参数，抄自库自己的 CHROME_DOCKER_ARGS
#   （browser/profile.py:130-132）。不自己发明，是为了让"探针验证过的组合"
#   和"库在 chromium_sandbox=False 时真正会用的组合"是同一个东西 ——
#   否则探针说"关沙箱就好了"，而库关沙箱时加的参数和探针不一样，白验。
SANDBOX_OFF_ARGS = ["--no-sandbox", "--disable-gpu-sandbox", "--disable-setuid-sandbox"]


async def _sandbox_matrix(
    browser_path: str, lib_args: list[str]
) -> dict[str, tuple[str, int | None, str]]:
    """用【库真正挑中的那个二进制】，沙箱开/关各起一次。

    ★ 为什么值得单独跑这个矩阵：
      崩溃栈落在 content::ZygoteHostImpl::Init()，它同时兼容两个完全不同的假设 ——
        A. 库挑的二进制和探针挑的不是同一个（两份不同的搜索清单，见
           local_browser_watchdog.py:264-279 vs browser/chrome.py）→ 修法是钉死路径；
        B. 二进制是同一个，是这个 runner 不让 Chrome 建 namespace 沙箱
           （Ubuntu 24.04 起 kernel.apparmor_restrict_unprivileged_userns=1）
           → 修法是 chromium_sandbox=False。
      这两个假设指向【不同的修法】，而它们在同一个崩溃栈下长得一模一样。
      与其猜，不如把两个变量一次跑完：结果直接决定改哪一行。
    """
    base = [a for a in lib_args[1:] if not a.startswith("--remote-debugging-port")]
    out: dict[str, tuple[str, int | None, str]] = {}
    for label, extra in (("沙箱开", []), ("沙箱关", SANDBOX_OFF_ARGS)):
        port = _port()
        verdict, err, code = await _try_launch(
            browser_path, [*base, f"--remote-debugging-port={port}", *extra], port
        )
        print(f"\n  [{label}] 结论={verdict} 退出码={code}")
        for ln in fatal_lines(err)[:4]:
            print(f"      {ln}")
        out[label] = (verdict, code, err)
    return out


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


def _forensics(log_path: str) -> int:
    """只读一份已经捕获的子进程输出，挑出死因行并抬成注解。

    ★ 为什么让 CI 调这个脚本，而不是在 workflow 里再写一遍 grep：
      "哪几行算死因"这件事原本有【两份实现】—— 本文件的 fatal_lines，
      和 ci.yml 里的 `grep -aE 'FATAL|Check failed|Received signal|zygote|sandbox'`。
      而且两者思路相反（一个黑名单、一个白名单），修好一份另一份还是错的，
      且【没人会发现，因为两边都跑得动】。
      CI 只该决定【什么时候】去看，不该决定【怎么看】。
    """
    p = Path(log_path)
    if not p.exists() or not p.read_text(encoding="utf-8", errors="replace").strip():
        _ann(
            "notice",
            "没有捕获到任何子进程输出 —— 说明库根本没走到起 Chrome 那一步，故障在更上游",
        )
        return 0

    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(">>> 第 ")
    fatals = fatal_lines(text)
    print(f"捕获到 {n} 次子进程启动；死因行 {len(fatals)} 条")
    for ln in fatals[:10]:
        print(f"  {ln}")
    if fatals:
        _ann(
            "error",
            f"Chrome 死因行（库把它接进 PIPE 却从不读，所以这份只在此处存在）。"
            f"共捕获 {n} 次子进程启动：" + " | ".join(fatals[:3]),
        )
    else:
        # ★ 没挑出死因行【不等于没问题】：这份文件是"失败时才来看"的，
        #   走到这里要么是失败发生在起 Chrome 之前，要么是 Chrome 说了句
        #   我们还不认识的话。两种都该被看见，不能安静地什么都不报。
        _ann(
            "warning",
            f"测试失败了，但这份捕获里挑不出死因行（共 {n} 次子进程启动）—— "
            "故障可能发生在起 Chrome 之前，或者是一种没见过的失败形态。",
        )
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--fatal-lines":
        return _forensics(argv[1] if len(argv) > 1 else "/tmp/chrome_capture.log")

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

    fatals = fatal_lines(log_text)
    if fatals:
        print("死因行（从崩溃转储里挑出来的，不是 tail）:")
        for ln in fatals[:6]:
            print(f"  {ln}")
        _ann("error", "Chrome 的死因行：" + " | ".join(fatals[:3]))

    lib_binary = str((lib.get("argv") or ["(未知)"])[0])
    if lib.get("argv"):
        print(f"\n⚠️ 探针自己用的是 {browser_path}")
        print(f"   库用的是        {lib_binary}")
        _ann("notice", f"库真正挑中的二进制: {lib_binary}")
        if lib_binary != str(browser_path):
            _ann(
                "warning",
                f"两者不是同一个二进制！探针={browser_path} / 库={lib_binary}"
                "（两处用的是不同的搜索清单：browser/chrome.py vs local_browser_watchdog.py:264-279）",
            )

    # ── 3b. 把"修法"也验掉：让库带着 chromium_sandbox=False 再起一次 ──
    # ★ 为什么要在探针里预演修复，而不是"先出结论、下个 commit 再改、再等一轮 CI"：
    #   那样拿到的是"我猜这个开关能修"，而这里拿到的是"这个开关在这个 runner 上
    #   确实能起"。两者都是好证据，但后者不需要再花一轮 5 分钟去确认，
    #   而且如果预演失败，我当场就知道假设错了 —— 不必等到改完代码才发现。
    lib2: dict = {}
    if lib.get("argv") and not lib.get("ok"):
        print("\n" + "─" * 70)
        print("第二段（对照）：同一份配置，但 chromium_sandbox=False")
        print("─" * 70)
        lib2 = asyncio.run(_library_launch({**session_kw, "chromium_sandbox": False}))
        print(f"结果 : {'成功' if lib2.get('ok') else '失败'}")
        print(f"异常 : {lib2.get('error') or '(无)'}")
        if lib2.get("ok"):
            _ann(
                "error",
                "对照结论：同一条库路径，chromium_sandbox=False 就起得来 → "
                "修法已在本 runner 上验证，直接给 CI 加这个开关即可",
            )
        else:
            _ann(
                "error",
                "对照结论：chromium_sandbox=False 也不行 → 关沙箱不是修法，别去改它。"
                f"异常={lib2.get('error') or '?'}",
            )

    # ── 4. 沙箱矩阵：用库挑中的二进制，开/关各起一次 ──────
    matrix: dict[str, tuple[str, int | None, str]] = {}
    if lib.get("argv"):
        print("\n" + "─" * 70)
        print("第三段：沙箱矩阵（用库真正挑中的二进制，其余参数照抄库）")
        print("─" * 70)
        matrix = asyncio.run(_sandbox_matrix(lib_binary, lib["argv"]))
        on_v, off_v = matrix["沙箱开"][0], matrix["沙箱关"][0]
        print(f"\n沙箱开={on_v}  沙箱关={off_v}")
        if off_v == "cdp_ok" and on_v != "cdp_ok":
            _ann(
                "error",
                "矩阵结论：同一个二进制，沙箱关了就起得来、开着就崩 → 修法是 BrowserSession(chromium_sandbox=False)"
                "（库的既有开关，profile.py:447，为 False 时自己会加上 --no-sandbox）",
            )
        elif on_v == "cdp_ok":
            _ann("notice", "矩阵结论：沙箱开着也能起 → 崩溃与沙箱无关，是库那条路径特有的差别")
        else:
            _ann("error", f"矩阵结论：开/关都起不来（开={on_v} / 关={off_v}）→ 与沙箱无关，去看死因行")

    # ── 5. 结论 ─────────────────────────────────────────
    if lib.get("ok"):
        _ann("notice", "第二段：库自己的启动路径也成功了 → 失败与启动方式无关，去查测试那份配置")
    elif matrix:
        _ann("error", f"第二段：库自己的启动失败（退出码 {lib.get('returncode')}，死因见 Chrome 死因行）")
    else:
        _ann("error", f"第二段：库自己的启动失败。异常={lib.get('error') or '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
