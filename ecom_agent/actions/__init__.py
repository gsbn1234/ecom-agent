"""自定义 action。

本项目的 action 只做两件事：**取数据**和**说拒绝**。
所有"改变页面状态"的动作都走库内置的（click / input / …），因为那些是 LLM 的常规手段，
而我们不需要在它们外面再包一层 —— 护栏拦的是**决策**，不是实现。

★★ `build_tools()` 住在这里，而不是住在 `guard_gate.py` 里：

  它是"本项目注册了哪些自定义 action"的**唯一清单**，所以它必须住在一个
  同时看得见所有 action 模块的地方。原先它长在 `guard_gate.py` 里，
  Phase 4 加 `extract_table` 时那个位置就立刻变成了谎话
  （一个叫 guard_gate 的模块里注册着取表格的动作）。

  ★ 而且它有个实际好处：**"注册了哪些动作"变成一个能被读出来的短清单**。
    这一点不只是好看 —— `extract_table` 之所以能被护栏的 `match_action` 引用，
    前提就是它出现在这张表里；而"规则引用了没注册的动作"这条规则会
    **永远不命中**（静默失效，见 runtime/runner.py 的 unrunnable_rule_actions）。
    清单短到一眼能看完，是这件事可维护的前提。
"""
from __future__ import annotations

from browser_use import Tools

from ecom_agent.actions.extract_cards import (
    CARDS_ACTION,
    ExtractCardsAction,
    extract_cards_impl,
    register_extract_cards,
    verify_extract_cards_round_trip,
)
from ecom_agent.actions.extract_table import (
    EXTRACT_TABLE_ACTION,
    ExtractTableAction,
    extract_table_impl,
    register_extract_table,
    verify_extract_table_round_trip,
)
from ecom_agent.actions.guard_gate import (
    GUARD_NOTICE_ACTION,
    make_notice_action,
    register_guard_notice,
    verify_notice_round_trip,
)
from ecom_agent.dsl.models import CardField

__all__ = [
    "CARDS_ACTION",
    "EXTRACT_TABLE_ACTION",
    "GUARD_NOTICE_ACTION",
    "CardField",
    "ExtractCardsAction",
    "ExtractTableAction",
    "build_tools",
    "extract_cards_impl",
    "extract_table_impl",
    "make_notice_action",
    "register_extract_cards",
    "register_extract_table",
    "register_guard_notice",
    "verify_extract_cards_round_trip",
    "verify_extract_table_round_trip",
    "verify_notice_round_trip",
]


def build_tools(
    *, exclude_actions: list[str] | None = None, card_fields: "list[CardField] | None" = None
) -> Tools:
    """造一个 Tools 实例，注册本项目的**全部**自定义 action。

    ★ 必须在**构造 Agent 之前**调用，并把结果通过 `Agent(tools=...)` 传进去。
      原因：`ActionModel` 是 Agent 构造时按注册表生成的
      （`AgentOutput.type_with_custom_actions(self.ActionModel)`，`service.py:786-790`）。
      注册晚了，那个模型里就没有对应的字段，注入动作时抛 ValidationError
      —— 那是**好事**（报错而不是静默），但报错发生在 run 已经跑起来之后。
      所以启动期还要各查一次：`verify_notice_round_trip()`、
      `verify_extract_table_round_trip()`、`verify_extract_cards_round_trip()`。

    ★★ `card_fields` 是从**任务定义**（`CompiledTask.card_fields`）传进来的，
      不是这里写死的：字段判据属于任务，不属于采集器。它走闭包进 action，
      于是 LLM 只读 —— 见 actions/extract_cards.py 顶部的说明。

    ★★ 为什么每个 action 由自己的模块提供 `register_xxx(tools)`，
      而不是在这里直接写 `@tools.action(...)`：
      动作名 = **函数名**（库用 `func.__name__`），所以装饰器必须紧挨着
      那个函数定义。把定义摊平到这个文件里，会让"这个动作的参数模型、
      JS、实现"分散到两个文件。所以这里只负责**编排顺序**，
      动作本身留给各自的模块。
    """
    tools = Tools(exclude_actions=list(exclude_actions or []))
    register_guard_notice(tools)
    register_extract_table(tools)
    register_extract_cards(tools, fields=list(card_fields or []))
    return tools
