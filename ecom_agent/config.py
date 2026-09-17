"""ecom-agent 全局配置中心。

为什么要有这个文件：它是所有模块的公共依赖。在这里把 dotenv 加载好、
把有歧义的开关归一化，等于对 main.py / webapp / devtools / tests 一次性全局生效。

★ 它放在包内（ecom_agent/config.py）而不是项目根目录（config.py）。
  根级模块要被 ecom_agent/* 导入，就得依赖"运行时 sys.path 里恰好有项目根"
  这个前提 —— 从 tests/ 跑、从 webapp/ 跑、被 pip 装成 wheel 后跑，前提各不相同。
  放进包内则 `from ecom_agent.config import ...` 在任何一种情形下都成立。
  （轮子只打包 ecom_agent/，根级 config.py 根本不会进 wheel —— 那才是真正的坑。）
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── 路径锚定 ──────────────────────────────────────────────
# ★ 以 config.py 自身位置为锚点推算项目根，而不是用 CWD 或相对路径。
#   这样从任何目录启动（CLI / pytest / 从 webapp 里 import）路径都稳定。
#   踩过的坑：用相对路径时，从 tests/ 跑和从项目根跑，找到的是两个不同的目录 ——
#   而且不会报错，只会在两个地方各建一个 runs/，事后要花时间才想明白。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
TASKS_DIR = PROJECT_ROOT / "tasks"
DB_PATH = RUNS_DIR / "ecom_agent.db"

# ★ 审批通道的文件约定目录。WebApprover 与 FileApprover 共用同一套：
#   pending/ 里有一份 = 有人正在等回答。进程重启后 pending/ 非空，
#   说明上一个 run 是在等审批时死掉的 —— 这个残留本身就是有用的诊断信号，
#   所以刻意不放在某个 run 的子目录里（那样它只会在该 run 被查看时才发现）。
APPROVALS_DIR = RUNS_DIR / "approvals"
PENDING_DIR = APPROVALS_DIR / "pending"
DECIDED_DIR = APPROVALS_DIR / "decided"

# ── 密钥 ──────────────────────────────────────────────────
# 裸读不校验，交给调用方。测试与 CI 用假 key（见 .github/workflows/ci.yml）。
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# ── 浏览器 ────────────────────────────────────────────────
# ★ 本机绝对路径只作「默认值」，且必须能被环境变量覆盖。
#   写死的话，这个项目在除开发机之外的任何地方都跑不起来 —— 包括 CI。
_DEFAULT_WIN_CHROME = (
    r"C:\Users\21702\AppData\Local\ms-playwright\chromium-1223\chrome-win64\chrome.exe"
)
CHROME_PATH = os.getenv("ECOM_AGENT_CHROME_PATH")
if CHROME_PATH is None:
    # 未设置（None）→ 用平台默认值。
    CHROME_PATH = _DEFAULT_WIN_CHROME if sys.platform == "win32" else ""
# ★ 区分「未设置」与「设为空串」：设成空串是明确要求 browser-use 自己探测，
#   不能被上面的默认值覆盖。
CHROME_PATH = CHROME_PATH.strip()

# ⚠️⚠️ 「设为空串让库自己探测」现在【只推荐在开发机上用】，CI 上一律显式钉死。
#   这是 2026-09-16 那轮 CI 全红换来的教训（完整记录见 docs/spikes.md）：
#
#   库里有【两份互不一致的清单】回答「哪个 Chrome」——
#     · browser/chrome.py:find_chrome_executable()  走 which，google-chrome 优先
#     · local_browser_watchdog.py:264-279（真正启动用的是这条）硬编码路径表，
#       chromium 组优先（第 321-323 行 patterns = prioritized + rest）
#   正常机器上两者挑中的是同一个二进制，所以看不出来；
#   一旦机器上两个都装了、而其中一个的沙箱助手不可用，差别就是
#   「CDP 就绪」与「SIGABRT，退出码 -6」的区别。GitHub runner 恰好就是这种情况：
#     FATAL:zygote_host_impl_linux.cc:129] No usable sandbox! ... 退出码 -6
#
#   所以：凡是我们能控制的环境（CI、容器、别人的机器），都必须显式赋值，
#   把「库挑哪个」这个不确定性从链路里摘掉。留空只在开发机上可接受 ——
#   因为那里我们随时能用 devtools/probe_browser.py 看出来它挑了什么。

HEADLESS = os.getenv("ECOM_AGENT_HEADLESS", "true").strip().lower() == "true"

# ── 持久登录 profile（Phase 6）────────────────────────────
# ★ 空 = **不使用**持久 profile：每次 run 一个临时 profile，无状态、可复现。
#   这是默认值，且必须是默认值 —— 另外那个选项是"复用一个带登录 cookie 的目录"，
#   是**有状态**的。把有状态的东西设成默认，等于让 CI 的 11 条浏览器用例
#   共用一个 cookie 目录：测试之间互相污染，而且只在特定执行顺序下才暴露出来
#   （同一台机器上两个 Chrome 用同一个 profile 目录，后起的那个会直接起不来）。
#
# ★ 为什么不能"想用的时候自己传路径"就完事：**登录脚本和 run 必须指向同一个目录**。
#   两边各传各的，不一致时不会有任何报错 —— 表现是"明明扫码登录过了，跑起来还是
#   登录页"，而那时人会去怀疑 cookie 过期、怀疑风控，方向从一开始就是错的。
#   所以这个值放在这里，两边都读它（CLI 的 --profile、devtools/login_pdd.py）。
USER_DATA_DIR = os.getenv("ECOM_AGENT_USER_DATA_DIR", "").strip()

# ★ 显式配置为空时的**约定位置**。它不参与 BrowserSession 的构造（空就是不传），
#   只用来在两处说同一句话：登录脚本默认写这儿，CLI 的警告也建议设成这儿。
#   单点定义是为了让这两句话永远一致 —— 否则用户照着警告设了值，
#   登录脚本却写去了另一个目录，就又回到上面那个静默的不一致。
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "browser_profile"
#   （`browser_profile/` 已在 .gitignore 里：里面是登录 cookie，泄漏 = 别人能登你的后台。）

# ── 是否允许真调 LLM ──────────────────────────────────────
# ★ 开关型配置的「双条件」：布尔 flag AND key 非空。缺一样就静默关闭、不报错，
#   因为 CI 和单测都应该在「没有真 key」的情况下正常跑完全部用例。
LIVE_LLM = (
    os.getenv("ECOM_AGENT_ENABLE_LIVE_LLM", "").strip().lower() == "true"
    and bool(DEEPSEEK_API_KEY)
)
if not LIVE_LLM and DEEPSEEK_API_KEY:
    logger.debug("检测到 DEEPSEEK_API_KEY 但未开启 ECOM_AGENT_ENABLE_LIVE_LLM，按离线模式运行")

# ── 护栏 ──────────────────────────────────────────────────
APPROVAL_TIMEOUT_S = int(os.getenv("ECOM_AGENT_APPROVAL_TIMEOUT_S", "300"))

# ★ 审批超时必须小于 action 超时，否则人工还没点「批准」，Tools.act 先把整个
#   action 掐了，表现为一个莫名其妙的 ActionResult(error=...) —— 排查时完全
#   看不出是超时配置问题。
#   这里主动把 action 超时抬到审批超时之上，而不是压低审批超时 ——
#   因为审批超时是给「人」的，压低了人来不及看。
#   注意：主路径（审批放在 new_step_callback 里）不经过 Tools.act，不受这个约束；
#   这是给回退方案 C（action 内 await 审批）兜底的。
os.environ.setdefault("BROWSER_USE_ACTION_TIMEOUT_S", str(APPROVAL_TIMEOUT_S + 30))
