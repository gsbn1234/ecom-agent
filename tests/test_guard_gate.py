"""`guard_notice` 的接线契约 —— 以及它的**对照实验**。

★★ 这个文件存在的唯一理由，是护栏最危险的那种失败形态：

    判定照做、记录照写、**动作照跑**。

  护栏拒绝一个动作的方式，是把它**换成**一条 `guard_notice(message=...)`。
  而那条 notice 必须是 Agent 的 `ActionModel` 里存在的一个字段 ——
  那个模型是 Agent 构造时按注册表生成的（service.py:786-790）。
  注册晚了（没把 `build_tools()` 的结果传给 `Agent(tools=...)`），
  `make_notice_action` 的 `model_validate` 会抛 ValidationError,
  而那时 run 已经跑起来了 —— 在 `new_step_callback` 里抛异常，
  表现是这一步被跳过、动作原地消失，日志上只有一条看不出所以然的报错。

  所以启动期必须查一次（`verify_notice_round_trip`），而这个文件负责证明
  **那个检查真的会失败**。一个永远通过的检查等于没有检查。
"""
from __future__ import annotations

import pytest
from browser_use import Tools

from ecom_agent import compat
from ecom_agent.actions.guard_gate import (
    GUARD_NOTICE_ACTION,
    build_tools,
    make_notice_action,
    verify_notice_round_trip,
)


def _action_model(tools):
    return tools.registry.create_action_model()


# ── 正例：注册了就是通的 ──────────────────────────────────
def test_build_tools_registers_guard_notice():
    tools = build_tools()
    model = _action_model(tools)
    assert GUARD_NOTICE_ACTION in compat.action_model_fields(model)


def test_round_trip_passes_with_the_real_tools():
    """★ 启动期门禁在正常配置下必须通过（否则每次 run 都起不来）。"""
    verify_notice_round_trip(_action_model(build_tools()))


def test_notice_action_keeps_its_message():
    """★ message 必须原样活到下游。

      丢了的话 LLM 只会看到一条**没有原因的拒绝** ——
      它不知道自己做错了什么，改道的概率大幅下降，
      而"护栏把话说清楚了"这件事在记录里是看不出来的（记录里 message 是我们的原文）。
    """
    instance = make_notice_action(_action_model(build_tools()), "这里是被拒绝的原因")
    dumped = instance.model_dump(exclude_unset=True)
    assert list(dumped) == [GUARD_NOTICE_ACTION]
    assert dumped[GUARD_NOTICE_ACTION]["message"] == "这里是被拒绝的原因"


def test_guard_notice_returns_an_error_not_a_done():
    """★★ 拒绝必须是 `ActionResult(error=...)`，**不是** `is_done=True`。

      `multi_act` 那一行是
      `if results[-1].is_done or results[-1].error or i == total_actions - 1: break`
      （agent/service.py:2818）—— 两种都能丢掉本批剩余动作，差别在之后：
        · error → run **继续**，LLM 下一步看到 error 并改道   ← 要的就是这个
        · is_done → run **结束**，整个任务死在一次误触上

      对一个只读采集任务，一次误触不该终结整个 run。

    ★ 必须 await：注册进 registry 的函数被包成了 async 包装器
      （`tools/service.py` 里 `act` 那条链路要 await 它）。
      直接同步调用拿到的是一个 coroutine 对象 —— 而它**不会报错**，
      只会在拿属性时抛一句"coroutine 没有 error 属性"，指不到真正的原因。

    ★★ 必须用**关键字**传参，位置传参会炸。库在
      `tools/registry/service.py:174` 的 normalized_wrapper 里明写：

          raise TypeError(f'{func.__name__}() does not accept positional
                           arguments, only keyword arguments are allowed')

      —— 因为 LLM 产出的动作参数永远是 JSON 对象（天然具名），
      库索性把这条约束提到了调用层。它对本项目的意义是：
      **任何我们自己去调自定义 action 的地方都得用 kwargs**，
      而用位置参数时那条报错看起来像"我的函数签名不对"。
    """
    import asyncio

    tools = build_tools()
    action = tools.registry.registry.actions[GUARD_NOTICE_ACTION]
    result = asyncio.run(action.function(message="(test) 被拒绝了"))

    assert result.error == "(test) 被拒绝了"
    assert not result.is_done, "拒绝如果触发 done，整个 run 会死在一处误触上"
    assert not result.success


# ── ★★ 对照实验：注册晚了会怎样 ───────────────────────────
def test_control_experiment_unregistered_registry_has_no_guard_notice_field():
    """★★ 对照实验的**前提**：没有注册我们的 action 时，那个字段真的不存在。

      没有这一条，下面那条"检查会失败"的测试就可能是**因为别的原因**失败的
      （比如 pydantic 版本问题），而我们就不知道自己验的到底是什么。
      先把"字段确实不在"钉住，失败才有确定的含义。
    """
    bare = _action_model(Tools())
    fields = compat.action_model_fields(bare)
    assert GUARD_NOTICE_ACTION not in fields
    # ★ 同时证明模型**不是空的**：空模型上"找不到 guard_notice"是废话。
    assert {"click", "navigate", "done"} <= fields


def test_control_experiment_verify_raises_when_registration_is_late():
    """★★★ 这就是"注册晚了会让护栏静默失效"的可执行证明。

      它同时证明启动期门禁**不是摆设** —— 一个永远通过的检查比没有检查更坏，
      因为它让人以为被管住了。

      ⚠️ 失败信息里必须**带出原始异常**：真正的原因（pydantic 的 48 条 union
        校验错）藏在里面。只报一句"无法构造 guard_notice"的话，
        排查的人会去查注册表，而真相可能是 Shape 变了。
    """
    with pytest.raises(RuntimeError) as ei:
        verify_notice_round_trip(_action_model(Tools()))
    message = str(ei.value)
    assert GUARD_NOTICE_ACTION in message
    assert "tools=" in message, "报错必须指出最可能的修法（把 build_tools() 传给 Agent）"
    assert "ValidationError" in message, "原始异常要带出来，否则真正的原因被吞了"


def test_control_experiment_round_trip_catches_message_loss(monkeypatch):
    """★ 第二个对照：即使字段在，**参数丢了**也要报错。

      只查"字段名在不在"是不够的 —— 类型不匹配、必填参数缺失都看不到，
      而那些情况下 LLM 收到的是一条没有原因的拒绝，与"完全没有护栏说明"等价。
    """
    import ecom_agent.actions.guard_gate as G

    real = G.make_notice_action

    def loses_message(action_model, message):
        inst = real(action_model, message)
        dumped = inst.model_dump(exclude_unset=True)
        dumped[GUARD_NOTICE_ACTION].pop("message", None)
        return type("Fake", (), {"model_dump": lambda self, **kw: dumped})()

    monkeypatch.setattr(G, "make_notice_action", loses_message)
    with pytest.raises(RuntimeError, match="参数"):
        G.verify_notice_round_trip(_action_model(build_tools()))


def test_action_model_is_a_rootmodel_wrapper_so_field_lookup_must_unwrap():
    """★ 记录一个**会让人误判**的形状。

      `create_action_model()` 的返回值在动作多于一个时是
      `RootModel[Union[...]]` 包装，`model_fields` 只有 `{'root'}` 一个键。
      直接读它得到"里面没有 guard_notice"—— 一次**误报**。
      假警报比不报警更坏：它训练人忽略这个检查。

      `compat.action_model_fields` 存在的唯一理由就是穿过这层包装。
      这里把两种读法的差别并排钉住，免得将来有人"简化"掉那个函数。
    """
    model = _action_model(build_tools())
    assert set(model.model_fields) == {"root"}, "包装形状变了 → action_model_fields 要跟着改"
    assert GUARD_NOTICE_ACTION in compat.action_model_fields(model)

    # 反面对照：直接读原始字段会误报
    assert GUARD_NOTICE_ACTION not in set(model.model_fields)
