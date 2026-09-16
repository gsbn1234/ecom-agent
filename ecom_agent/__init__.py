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

__version__ = "0.1.0"
