"""ecom-agent：基于 browser-use 二次开发的电商卖家后台自动化助手。

分层（越靠上越依赖浏览器，越靠下越纯）：

    runtime/      Agent 组装与运行（唯一 import browser_use 的地方之一）
    actions/      自定义 action（extract_table / guard_gate）
    guardrails/   三层护栏：规则引擎（纯）+ 策略（纯）+ 审批通道（async）
    dsl/          YAML 任务模板 → CompiledTask（纯函数，零 IO 零浏览器）
    observability/逐步记录、脱敏、报告
    store/        SQLite 落库
    sites/        站点适配：URL、DOM 语义提示、输出模型
    compat.py     ★ 所有对 browser-use 脆弱行为的唯一访问点

设计主线见 docs/ADR.md。
"""
import os as _os

# ── 遥测：在本项目里一律关掉 ──────────────────────────────
# ★ 为什么这件事放在包的最顶层（而不是 config.py）：
#   browser-use 的 CONFIG 是【它自己的模块被 import 时】读环境变量生成的一个单例
#   （`browser_use/config.py:58` 读 `ANONYMIZED_TELEMETRY`，默认 'true'）。
#   一旦 `import browser_use` 发生过，再设环境变量就没用了 ——
#   而设晚了的失败形态是**没关掉而且看不出来**（它照常上报，只是你以为关了）。
#
#   `ecom_agent/__init__.py` 在任何 `ecom_agent.*` 子模块之前执行，
#   而本项目所有会 import browser_use 的代码都在 `ecom_agent/` 下面，
#   所以只要走正常入口就一定是这里先跑。
#
# ★ 为什么这个项目要关：
#   它操作的是**卖家自己的后台**。即便遥测是匿名的，"一个自动化工具在什么时候
#   访问了哪个后台"本身就是不该外泄的信息。而且 CI 里每次跑测试都发一次匿名请求，
#   是纯噪音加偶发失败源。
#
#   BROWSER_USE_CLOUD_SYNC 默认跟随 ANONYMIZED_TELEMETRY（config.py:63）——
#   显式一并关掉，因为"它会跟随"是一个隐式依赖，写出来更清楚。
#
# ★ setdefault 而不是直接赋值：使用者若明确想开（自己排查问题），
#   仍可用 ANONYMIZED_TELEMETRY=true 覆盖，而不是被我们锁死。
#   ⚠️ 已知边界：调用方若在 import ecom_agent 之前就 import 了 browser_use，这里来不及。
#      tests/test_telemetry_off.py 只把"正常路径下关掉了"钉住，
#      **不假装覆盖了那条边界**。
_os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
_os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

__version__ = "0.1.0"
