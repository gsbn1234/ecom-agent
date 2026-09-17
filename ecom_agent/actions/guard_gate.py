"""`guard_notice` —— 把"这个动作被拒了"变成一条 LLM 看得见的消息。

★★ 为什么需要一个 action，而不是直接把被拒的动作从列表里删掉：

  删掉之后 `model_output.action` 变空，LLM 在下一步看到的上下文是"我上一轮没下达任何指令"。
  它的反应是**把同一个危险动作再下一次** —— 于是变成死循环：
  每一轮都被拒、每一轮都重试，直到撞上 max_failures 或 max_consecutive_blocks。

  换成一条明确的说明则不同：LLM 知道"系统拒绝了它"，才有可能改道
  （换一个元素、换一种方式、或者直接 done 汇报受阻）。
  **这是"拒绝"与"沉默"的区别**，而沉默在对话式系统里必然被解读成"没收到"。

★★ 为什么返回 `ActionResult(error=...)` 而不是 `is_done=True`：

  `multi_act` 里那一行是 `if results[-1].is_done or results[-1].error or i == total_actions - 1: break`
  （`agent/service.py:2818`）。两种都能丢掉本批剩余动作，差别在于之后：
    · `error` → run **继续**，LLM 下一步看到 error 并改道 ← 要的就是这个
    · `is_done=True` → run **结束**，整个任务死在一次误触上
  对一个"只读采集"的任务，一次误触不该终结整个 run。

  ⚠️ 代价必须说清楚：error 会累加 `state.consecutive_failures`。
     所以护栏**不能**只靠库的 max_failures 来兜底 —— 它会因为别的原因（网络抖动）先被耗尽。
     拦截器自己维护 `policy.record_block()` 的连续计数，到上限就硬停（见 interceptor.py）。
"""
# ★★ 本模块【刻意】不写 `from __future__ import annotations`。
#   库在**注册期**用 inspect 读注解并跟自己那份特殊参数类型表做 `==` 比较，
#   PEP 563 会把注解变成字符串，于是比较恒为 False、注册直接抛一句
#   "conflicts with special argument injected by tools: 'browser_session: BrowserSession'"
#   —— 报错两边长得一模一样，极难定位。
#
#   ⚠️ 这个坑**取决于 action 有没有特殊参数**，不取决于有没有写那行 import：
#      本模块的 guard_notice 只收一个 message，所以带着 future import 也能跑。
#      正因如此，照着本模块抄一个**带 browser_session 的** action 就会炸。
#      完整实测与对照实验记在 extract_table.py 顶部。
#
#   统一的规矩：**凡是定义 action 的模块，都不写 future import。**
from typing import Any

from browser_use import ActionResult, Tools

GUARD_NOTICE_ACTION = "guard_notice"
"""动作名。★ 拦截器要按这个名字去 ActionModel 上找字段，所以它是契约不是文案。"""


def make_notice_action(action_model: Any, message: str) -> Any:
    """造一个 `guard_notice(message=...)` 动作实例，可直接塞进 `model_output.action`。

    ★★ 为什么是 `model_validate({动作名: 参数})` 而不是"空模型 + setattr"：

      最初的版本照抄了库自己在 `agent/service.py:1690-1698` 那段
      （`action_instance = self.ActionModel()` + `setattr(...)`）。
      **实测证明那段在这个版本上根本跑不起来**，照抄会一起坏：

        · `self.ActionModel` 在动作多于一个时是 `RootModel[Union[...]]` 包装，
          零参构造直接抛 ValidationError（25 个成员各报一条：
          "Input should be a valid dictionary or instance of XxxActionModel"）——
          它没有 `root` 可填。
        · 那个包装类也**没有** `__setattr__` 代理，所以即便构造出来，
          `setattr(inst, 'guard_notice', ...)` 也会被 pydantic 拒掉
          （它只代理了 `get_index`/`set_index`/`model_dump` 三个方法）。
        · 单动作形态（只注册了一个 action 时）同样不能零参构造：
          它的那个字段是**必填**的。

      也就是说库那段 noop 注入代码在 0.13.10 上是死代码。**不能跟它保持一致**，
      因为一致的代价是"我们的拒绝注入也一起坏"。

    ★ 走 `model_validate` 的额外好处：**产出的对象与 LLM 产出的动作是同一个类型**
      （`ActionModelUnion`，`root` 是 `GuardNoticeActionModel`）。
      对照实验（同一份参数，两种造法，dump 整个 `AgentOutput`）：
        · `model_validate` → 无警告；
        · 直接塞单动作模型实例 → pydantic 抛
          `PydanticSerializationUnexpectedValue`（期望的是 ActionModelUnion）。
      功能上两种都能跑（下游只读 `model_dump(exclude_unset=True)`），
      但序列化警告是"训练人忽略输出"的那种噪音，所以用前者。

    ★ 校验也顺带成了我们的契约检查：`message` 必须是 str，
      这个约束来自 `guard_notice` 自己的签名，不是我们手写的。
    """
    return action_model.model_validate({GUARD_NOTICE_ACTION: {"message": message}})


def register_guard_notice(tools: Tools) -> None:
    """把 `guard_notice` 注册进一个**已有的** Tools 实例。

    ★ 它由 `ecom_agent/actions/__init__.py` 的 `build_tools()` 统一编排调用。
      为什么清单在那边而不在这里：那里同时看得见所有 action 模块，
      而"本项目注册了哪些动作"必须是一个能一眼读完的清单
      （护栏的 `match_action` 引用了没注册的动作时，那条规则会**永远不命中**）。

    ★★ 装饰器挂在**实例**上（`tools.action`）而不是类上（`Tools.action`）：
      `Tools.action` 是实例方法（`tools/service.py:2100`
      `def action(self, description, **kwargs)`），它只是转发给
      `self.registry.action(...)`。写成 `@Tools.action("...")` 时，
      那个字符串会被当成 `self` 传进去，报
      `TypeError: Tools.action() missing 1 required positional argument: 'description'`
      —— 报错信息指向 description 缺失，而真正的问题是 self 被写成了字符串，
      照着报错去补 description 只会越修越远。
      所以顺序必须是：**先有 Tools 实例，再注册**。
    """

    @tools.action(
        "系统拒绝了一个动作时用它说明原因。你不应该主动调用它。",
        # ★ terminates_sequence 保持默认 False。
        #   它是"执行后丢掉本批剩余动作"的开关，而我们的拒绝路径靠
        #   ActionResult.error 达到同样效果（见文件头）。两个开关同时打开会让
        #   "为什么后面的动作没执行"变成有两个可能原因，排查时多一层无谓的不确定性。
    )
    def guard_notice(message: str) -> ActionResult:
        return ActionResult(error=message)


def verify_notice_round_trip(action_model: Any) -> None:
    """启动期门禁：真的造一个 `guard_notice` 出来，看它能不能被下游正确读回。

    ★★ 为什么不是"检查字段名在不在"：
      那条路我已经踩过 —— `agent.ActionModel` 在多动作时是 `RootModel[Union[...]]`
      包装，`model_fields` 只有 `{'root'}`，于是"没有 guard_notice"这个判断
      **会误报**。假警报会训练人忽略这个检查，比不检查更坏。
      （`compat.action_model_fields` 现在能正确穿过包装，但"看得到字段名"
       仍然不等于"注入能成功" —— 类型不匹配、必填参数缺失都看不到。）

    ★ 所以这里做的是**端到端往返**，四步都不省：
        1. 造一个真实的 notice 实例；
        2. 按库读取动作名的**原样**取一次
           （`next(iter(model_dump(exclude_unset=True)))`，`service.py:2755-2756`）；
        3. 断言取回来的就是 `guard_notice`——不是 'unknown'，也不是别的动作；
        4. 断言参数里的 message 原样还在（否则 LLM 只会看到一条空说明）。
      这四步任何一步不成立，护栏的拒绝就是在**静默失效**：
      判定照做、记录照写，而动作照跑。

    失败就抛 —— 启动失败远好过"护栏看起来在跑"。
    """
    probe = "启动自检：这条 notice 不会被任何 run 使用"
    try:
        instance = make_notice_action(action_model, probe)
        dumped = instance.model_dump(exclude_unset=True)
    except Exception as exc:  # noqa: BLE001 —— 失败原因要原样带出去，不吞
        raise RuntimeError(
            f"无法构造 {GUARD_NOTICE_ACTION} 动作：{type(exc).__name__}: {exc}。"
            f"最常见的原因是在构造 Agent 之前没有把 build_tools() 的结果传给 tools=。"
            f"不修的话护栏的拒绝会静默失效。"
        ) from exc

    name = next(iter(dumped), None)
    if name != GUARD_NOTICE_ACTION:
        raise RuntimeError(
            f"{GUARD_NOTICE_ACTION} 动作往返后动作名变成了 {name!r} —— "
            f"下游会把它当未知动作，拒绝静默消失。dump 结果：{dumped!r}"
        )
    if (dumped.get(GUARD_NOTICE_ACTION) or {}).get("message") != probe:
        raise RuntimeError(
            f"{GUARD_NOTICE_ACTION} 的参数在往返中丢失或变形：{dumped!r} —— "
            f"LLM 会收到一条没有原因的拒绝说明。"
        )
