"""自定义 action。

本项目的 action 只做两件事：**取数据**和**说拒绝**。
所有"改变页面状态"的动作都走库内置的（click / input / …），因为那些是 LLM 的常规手段，
而我们不需要在它们外面再包一层 —— 护栏拦的是**决策**，不是实现。
"""
from ecom_agent.actions.guard_gate import GUARD_NOTICE_ACTION, build_tools, make_notice_action

__all__ = ["GUARD_NOTICE_ACTION", "build_tools", "make_notice_action"]
