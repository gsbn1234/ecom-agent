"""事件总线的契约 —— Phase 5 的 SSE 底座。

★★ 为什么这个模块值得单独一份测试，而且大半用例是"时序/并发"型的：
  事件总线的失败模式几乎全是**静默**的。丢一条事件、给一个订阅者少发一次、
  关闭信号没送到 —— 这些都不会报错，也不会让任何 HTTP 状态码变红。
  它们的表现只是"看板上少了点什么"，而"少了点什么"和"本来就没有"长得一样。

  所以这里的每条用例针对的都是一个**具体的、会静默发生的**失败：
    · 发布与订阅交错 → 事件两头都没有（既不在快照里也没投递）
    · 慢订阅者 → 内存无限涨；或者丢最新的一条（那是"看板停止更新"）
    · 慢订阅者漏了几十条 → 一路读到连接关闭都不知道自己漏了
    · close 不推哨兵 → 订阅者永远挂着（浏览器转圈，服务端认为一切正常）
    · 事件名拼错 → 前端 addEventListener 挂的名字永远不触发
    · 实时载荷与盘上不一致 → 回放看到的历史和当时看到的不一样

★ 全部零依赖：没有浏览器、没有 LLM、没有网络（asyncio 是标准库的）。

★★ 一个写这类用例的通用套路，本文件反复用到：**订阅者必须先真注册上**。
  `bus.subscribe()` 是 async generator —— 调用它**一行代码都不执行**，
  body 要等第一次 `__anext__` 才跑，而"注册进订阅集合"就在 body 里。
  所以"先 publish 再 subscribe"测的其实是 backlog，不是投递。
  要测投递，就得用一个消费任务 + `await asyncio.sleep(0)` 把它推到
  `await q.get()` 上停住 —— 那一刻它才是"已经在线的订阅者"。
  这个区别很容易搞混，而搞混的后果是**用例假通过**（它测的是另一条路）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ecom_agent.observability.events import (
    QUEUE_MAX,
    EventBus,
    RunEvent,
    events_from_run_dir,
)


def _ev(type_: str = "step_completed", **data) -> RunEvent:
    return RunEvent(type=type_, run_id="r1", data=data or {"step": 1})


async def _online(bus: EventBus, seen: list[RunEvent]) -> asyncio.Task:
    """起一个消费任务，并**等它真的注册进订阅集合**再返回。

    ★ 没有这个 `sleep(0)`，下面所有 publish 都会发生在"订阅者还不存在"的世界里，
      于是用例测的是 backlog，而不是本条用例想测的投递/溢出。
    """

    async def consume() -> None:
        async for e in bus.subscribe():
            seen.append(e)

    task = asyncio.create_task(consume())
    # 让消费任务跑到 `await q.get()` 上停住 —— 那时它已经在集合里了。
    await asyncio.sleep(0)
    assert bus._subscribers, "消费任务没能注册进订阅集合（sleep(0) 不够？）"
    return task


# ══════════════════════════════════════════════════════════
# 载荷形状
# ══════════════════════════════════════════════════════════
def test_a_typo_in_the_event_name_explodes_at_construction():
    """★★ 事件名是 `Literal`，所以拼错在**构造那一刻**就炸。

    ★ 这条针对的失败形态特别阴：发布点写了 `step_complete`（少一个 d），
      而前端 `addEventListener("step_completed", ...)` 挂的是正确名字 ——
      于是**那类事件一条都不显示**，而 SSE 连接 200、后端日志正常、
      浏览器控制台安静。整个链路上没有任何一处会说话。

    ★ 对照实验：先证明**正确**的名字能用，再证明错的那个炸。
      只测"错的会炸"的话，一个连正确名字都炸的实现也能通过。
    """
    ok = RunEvent(type="step_completed", run_id="r1")
    assert ok.type == "step_completed"

    with pytest.raises(Exception) as ei:
        RunEvent(type="step_complete", run_id="r1")  # type: ignore[arg-type]
    # ★ 断言消息里出现那个**错误的名字**，证明是 Literal 校验抓的，
    #   而不是别的什么原因（比如缺了必填字段）。
    assert "step_complete" in str(ei.value), f"报错里读不出是哪个名字不对：{ei.value}"


def test_the_sse_frame_is_three_fields_and_the_data_is_one_line():
    """SSE 帧的形态：`id:` / `event:` / `data:`，且 data **必须是单行**。

    ★ 为什么单行是硬要求：SSE 的帧分隔符就是空行。data 里出现一个裸换行，
      这一帧就在那里**提前结束**，后面半截被浏览器当成新字段 ——
      表现是"事件收到了但内容是残缺的"，而且不会报错。

    ★ 中文必须原样出来（`ensure_ascii=False`）。这一条和上一条不矛盾：
      json.dumps 会把字符串里的换行转义成 `\\n` 两个字符，所以
      "中文可读"和"帧不被内容破坏"可以同时成立。
      反过来写成 ensure_ascii=True 的话，页面上全是 \\uXXXX ——
      而"让看板显示人话"正是这个看板存在的理由（元素文本是给人读的）。
    """
    ev = RunEvent(
        type="step_completed",
        run_id="r1",
        seq=7,
        data={"提示": "中文", "多行": "第一行\n第二行", "价格": "¥59.90"},
    )
    frame = ev.sse()

    assert frame.endswith("\n\n"), "帧必须以空行结束 —— 那是 SSE 的分隔符"
    assert frame.startswith("id: 7\n"), frame
    assert "event: step_completed\n" in frame
    assert frame.count("\n\n") == 1, f"一个帧里出现了多个分隔符：{frame!r}"

    _, _, payload = frame.partition("data: ")
    # ★ 关键：内容里的换行被转义了，所以整帧仍然只有三段（+空行）。
    assert "\\n" in payload, "内容里的换行没有被转义 —— 它会破坏帧结构"
    assert "中文" in payload, f"中文被转义了（ensure_ascii 没关）：{payload!r}"
    body = json.loads(payload.strip())
    assert body["多行"] == "第一行\n第二行", "转义之后读回来必须是同一段文本"
    assert body["价格"] == "¥59.90"


# ══════════════════════════════════════════════════════════
# seq：去重、丢事件检测、SSE 的 id
# ══════════════════════════════════════════════════════════
async def test_seq_starts_at_one_and_never_repeats():
    """★ seq 一个字段干三件事：SSE 的 `id:`（断线重连锚点）、前端去重
    （EventSource 重连会重发 backlog）、以及**丢事件检测**（跳号 = 漏了）。

    所以它不能重复、不能跳号、必须从 1 开始。
    """
    bus = EventBus(run_id="r1")
    seqs = [bus.publish(_ev()).seq for _ in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


async def test_publishing_after_close_is_ignored_not_an_error():
    """★ 关闭之后还发事件是**正常的竞态**（比如 finally 里的清理日志），
    为它抛异常会把一次正常的收尾变成失败。丢掉即可。
    """
    bus = EventBus(run_id="r1")
    bus.publish(_ev())
    bus.close()
    late = bus.publish(_ev())
    assert late.seq == 0, "关闭之后的事件不该被分配 seq（它没有入总线）"
    assert len(bus.backlog()) == 1
    assert bus.closed


# ══════════════════════════════════════════════════════════
# 订阅：不丢、不重
# ══════════════════════════════════════════════════════════
async def test_backlog_then_live_without_gap_or_duplicate():
    """★★ 这条是本模块**最要紧**的一条，对应 subscribe() 里那个顺序约束。

    场景：先发布了 3 条，然后一个订阅者接上来，接着又发布 2 条。
    预期：订阅者恰好收到 5 条、顺序正确、seq 恰好是 1..5。

    ★ 失败形态（顺序写反时）：先取 backlog 快照、再进订阅集合 ——
      两步之间发布的那条**两头都没有**（快照里没有它，投递时订阅者还不在
      集合里）。于是 seq 出现 1,2,3,5 —— 少了一条，而且没有任何报错。
    ★ 另一种失败形态（重复投递）：看板上同一个步骤出现两次。
    """
    bus = EventBus(run_id="r1")
    for i in range(3):
        bus.publish(_ev(i=i))

    got: list[RunEvent] = []
    task = await _online(bus, got)
    bus.publish(_ev(i=3))
    bus.publish(_ev(i=4))
    bus.close()
    await asyncio.wait_for(task, timeout=2)

    assert [e.seq for e in got] == [1, 2, 3, 4, 5], (
        f"订阅者收到的 seq 是 {[e.seq for e in got]}，预期恰好 1..5 且不缺不重。"
        f"缺一条 → subscribe() 里『先进订阅集合、再取 backlog 快照』的顺序被写反了。"
    )
    assert [e.data["i"] for e in got] == [0, 1, 2, 3, 4]


async def test_two_subscribers_each_get_everything_once():
    """★ 一个订阅者慢不该让另一个少看内容（丢事件只丢给慢的那一个）。"""
    bus = EventBus(run_id="r1")
    a: list[RunEvent] = []
    b: list[RunEvent] = []
    ta = await _online(bus, a)
    tb = await _online(bus, b)
    for _ in range(4):
        bus.publish(_ev())
    bus.close()
    await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2)

    assert [e.seq for e in a] == [1, 2, 3, 4]
    assert [e.seq for e in b] == [1, 2, 3, 4]


async def test_a_subscriber_that_leaves_is_dropped():
    """★ 订阅者退出时必须从集合里摘掉（subscribe 的 finally）。

    不摘的话，每一次页面刷新都留下一个死队列，总线会往它们身上一直
    put_nowait —— 直到每个都填满 QUEUE_MAX 然后开始计入 dropped，
    于是"丢弃计数"这个要显示给人的数字会一路虚涨，
    而真正的问题是**没有人再读了**。

    ★★ 这条用例第一版是红的，而红的原因值得单独记一句：
      最初写成 `async for _ in bus.subscribe(): return`，然后断言集合为空。
      它失败了 —— 因为 **`async for` 的提前退出（break/return）不会立刻
      关闭 async generator**，`finally` 要等垃圾回收或事件循环的
      asyncgen 终结钩子才会跑。也就是说，那一版测到的是"GC 还没轮到"，
      而不是"集合没被清理"。
      → 所以这里用显式 `aclose()`，把"退出"变成一个确定的动作。
      （生产路径同理，所以 app.py 的 `_sse_frames` 也用 `aclosing` 显式关 ——
      见那里的注释。）
    """
    bus = EventBus(run_id="r1")
    bus.publish(_ev())

    agen = bus.subscribe()
    first = await agen.__anext__()  # 走完 body 前半段：注册 + 补 backlog
    assert first.seq == 1
    assert len(bus._subscribers) == 1, "订阅者没注册上，后面这条断言就没意义了"

    await agen.aclose()
    # ★ 读私有属性是刻意的：这条用例要验的正是"内部集合被清理了"。
    assert bus._subscribers == set(), f"退出后订阅集合非空：{bus._subscribers}"


# ══════════════════════════════════════════════════════════
# 慢订阅者：丢最旧的，并且**真的**说出去了
# ══════════════════════════════════════════════════════════
async def test_a_slow_subscriber_loses_the_oldest_and_gets_told():
    """★★ 队列满了之后的三条行为，全部是刻意的。

      1. **丢最旧的**，不是丢最新的。慢客户端最需要的是"现在在干嘛"；
         它已经错过的那些可以从盘上补（回放接口）。
         丢最新的表现是看板永久定格在一个过去的时刻 —— 和坏了没区别。
      2. **推一条 `stream_gap`**，让前端知道该去盘上补。
         一个"悄悄丢事件的看板"和"实时看板"在页面上长得一模一样。
      3. **只丢给这一个订阅者**，另一个跟得上的不受影响。

    ★★ 第 2 条曾经是坏的，而这条用例正是为它写的：第一版把缺口通知
      "尝试塞进队列，塞不进就算了"，但调用点在 `except QueueFull` 里面 ——
      那里队列**必然是满的**（刚丢掉一条、又塞进一条），所以那个分支
      永远不可达，`stream_gap` 一条都没发出去过。
      详见 `events.py` 里 `_Subscriber` 的 docstring（以及下面 seq=0 那条断言）。

    ★ 算术（queue_max=4，推到 e10，然后 close）：
        e5..e10 各挤掉一条最旧的 → missed=6 / dropped=6
        队列停在 [e7,e8,e9,e10]；close 清一个位置塞哨兵 → [e8,e9,e10,CLOSE]
        订阅者收到：[gap(missed=6), e8, e9, e10]
      这些数字是**确定**的（单线程、publish 全同步），所以可以精确断言。
    """
    bus = EventBus(run_id="r1", queue_max=4)
    seen: list[RunEvent] = []
    task = await _online(bus, seen)

    for i in range(10):
        bus.publish(_ev(i=i))
    # ★ 这里**不** await：publish 全同步，消费者得不到执行机会，
    #   队列才会真的溢出。中间插一个 await 就永远测不到溢出了。
    assert bus.dropped == 6, f"溢出算术不对：dropped={bus.dropped}"
    bus.close()
    await asyncio.wait_for(task, timeout=2)

    types = [e.type for e in seen]
    assert types[0] == "stream_gap", (
        f"第一条不是缺口通知，而是 {types[0]} —— 通知必须在它读到更多数据**之前**"
        f"到，否则前端会先渲染出一段紧挨着空洞的历史。收到的是 {types}"
    )
    assert types.count("stream_gap") == 1, (
        f"缺口通知应当**合并成一条**（丢了 6 条 → 一条通知），"
        f"实际 {types.count('stream_gap')} 条。每丢一条插一条的话，"
        f"慢客户端会被一堆'你好慢'的通知淹没。"
    )

    gap = seen[0]
    assert gap.data["dropped_to_this_subscriber"] == 6
    assert gap.data["dropped_total"] == 6
    # ★ 缺口通知的 seq 是 0 —— 它不占序号。前端的去重是 `if (ev.seq)`，
    #   所以 seq=0 天然跳过去重（两条缺口通知不会被当成重复而丢掉一条），
    #   而且不会推进 lastSeq（否则它会掩盖真正的跳号）。
    assert gap.seq == 0, "缺口通知不该占一个 seq —— 它不是一个新事实"

    steps = [e for e in seen if e.type == "step_completed"]
    assert [e.data["i"] for e in steps] == [7, 8, 9], (
        f"留下的应该是**最新**的几条，实际 {[e.data['i'] for e in steps]}"
    )
    assert steps[-1].data["i"] == 9, "最新的那条被丢掉了 —— 看板会定格在过去"


async def test_the_queue_cap_is_a_strategy_number_in_a_sane_range():
    """★ 512 这个值本身不是被测的**性质**，是一个策略数字。

    所以这里只钉"它在合理量级内"（按一步 1–3 KB 算，512 条约 1 MB）。
    真正要防的是有人把它改成 1（看板每隔一步就缺口）或 10**9（一个卡死的
    客户端就能把服务端内存拖爆）—— 那两种改动都会在这条用例上暴露。
    """
    assert 64 <= QUEUE_MAX <= 4096, f"QUEUE_MAX={QUEUE_MAX} 越出了合理量级"


# ══════════════════════════════════════════════════════════
# close：必须是"叫醒"，不是"删除"
# ══════════════════════════════════════════════════════════
async def test_close_wakes_up_waiting_subscribers():
    """★★ 这条防的是一个最隐蔽的表现：**浏览器一直转圈，服务端认为一切正常**。

    如果 close() 只是把订阅者从集合里摘掉或不做事，订阅者的
    `await q.get()` 会永远挂着 —— 而那个 await 在 SSE 生成器里，
    所以 HTTP 连接永远不结束、永远不会报错。用户看到的是加载动画不停。

    ★ 所以断言的是"订阅者**收到了终止**"，而不仅仅是"closed 是 True"：
      后者只证明标志位翻转了，证明不了有人被叫醒。
    ★ 用真超时（`wait_for`）而不是裸 `await task`：bug 存在时后者会**永远挂住**，
      于是这条用例变成"CI 卡死"而不是"红"。红是有信息的，卡死没有。
    """
    bus = EventBus(run_id="r1")
    got: list[RunEvent] = []
    task = await _online(bus, got)
    bus.publish(_ev(i=0))
    bus.close()
    await asyncio.wait_for(task, timeout=2)
    assert [e.seq for e in got] == [1], "关闭前那条事件也必须在"


async def test_close_works_even_when_the_subscriber_never_read_anything():
    """★ 队列满时也必须关得掉：清一个位置出来塞关闭哨兵。

    这条路径上"少一条历史事件"远好于"连接永远不关" ——
    但必须有人钉住它，否则一个"满了就塞不进去"的实现会静默存在。
    （queue_max=1 时被挤掉的是唯一那条事件，见 close() 的注释。）

    ★ 这里只断言**能结束**，不断言内容：这条路径的目标是"不挂死"。
    """
    bus = EventBus(run_id="r1", queue_max=1)
    seen: list[RunEvent] = []
    task = await _online(bus, seen)
    for i in range(5):
        bus.publish(_ev(i=i))
    bus.close()
    try:
        await asyncio.wait_for(task, timeout=2)
    except asyncio.TimeoutError:
        pytest.fail("队列满时 close() 没能终止订阅者 —— SSE 连接会永远挂着")

    # ★ 而且它仍然被告知"你漏了东西"，没有静悄悄地被关掉。
    assert any(e.type == "stream_gap" for e in seen), (
        f"队列满 + 关闭时，订阅者一条缺口通知都没收到（收到 {[e.type for e in seen]}）—— "
        f"它会一路读到连接关闭，却永远不知道自己漏了几十条。"
    )


# ══════════════════════════════════════════════════════════
# ★★ 回放与实时：同一个形状
# ══════════════════════════════════════════════════════════
def _write_run(run_dir: Path, *, steps: list[dict], record: dict | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "steps.jsonl").open("w", encoding="utf-8") as fh:
        for s in steps:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    (run_dir / "run.json").write_text(
        json.dumps(
            record
            or {
                "run_id": run_dir.name,
                "task_id": "t1",
                "task_name": "任务",
                "status": "completed",
                "parse_status": "ok",
                "rows_collected": 2,
                "steps": len(steps),
                "duration_s": 12.5,
                "errors": [],
                "started_at": "2026-09-17T00:00:00+00:00",
                "finished_at": "2026-09-17T00:00:12+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_replay_step_payload_is_the_disk_line_itself(tmp_path: Path):
    """★★★ 这是"回放不是另做一套渲染"的**可执行判据**。

    实时推的 `step_completed` 载荷是 `StepRecord.model_dump(mode="json")`
    经脱敏后的结果；盘上那一行是 `redactor.obj(record.model_dump(mode="json"))`。
    两者必须**逐字段相等** —— 否则前端就得为两个通道各写一套渲染，
    而两套迟早会漂移，漂移的表现是"回放看到的历史和当时看到的不一样"。

    ★ 判据刻意写成 `==` 而不是"包含关键字段"：子集相等是**允许字段变少**的，
      而字段变少正是漂移最常见的形式（新增一栏只加在了实时那份上）。
    ★ 这里用的是**盘上真有的那一行**（含 guardrail_decisions / 截图路径 /
      tokens），不是手搓的简化数据 —— 简化数据会让"字段少了一个"这类漂移
      恰好测不出来。
    """
    step = {
        "run_id": "r1", "task_id": "t1", "attempt": 1, "step": 1,
        "url": "http://127.0.0.1:8765/goods/goods_list",
        "title": "商品管理",
        "actions": [{"name": "click", "params": {"index": 7}, "element_text": "编辑"}],
        "results": [{"is_done": False, "success": True, "error": None,
                     "extracted_content_preview": None}],
        "guardrail_decisions": [
            {"rule_id": "confirm-edit", "decision": "confirm", "reason": "写入类操作",
             "matched_rule_ids": ["confirm-edit"], "approved": False,
             "approved_by": "web", "decided_at": "2026-09-17T00:00:03+00:00"}
        ],
        "model_thought": "点编辑进详情页", "tokens_in": 1200, "tokens_out": 40,
        "screenshot_path": "screenshots/a01/step-001.png",
        "same_frame_as_previous": False, "snapshot_missing": False,
        "actions_not_executed": False,
    }
    _write_run(tmp_path / "r1", steps=[step])

    events = events_from_run_dir(tmp_path / "r1")
    steps_ev = [e for e in events if e.type == "step_completed"]
    assert len(steps_ev) == 1
    got = steps_ev[0].data
    assert got == step, (
        "回放的 step_completed 载荷和盘上那一行不相等 —— 两套构造开始漂移了。\n"
        f"  只在盘上：{sorted(set(step) - set(got))}\n"
        f"  只在事件里：{sorted(set(got) - set(step))}\n"
        f"  值不同：{sorted(k for k in set(step) & set(got) if step[k] != got[k])}"
    )


def test_replay_does_not_invent_guardrail_or_approval_events(tmp_path: Path):
    """★★ 这条钉的是一个**刻意的缺席**，所以它读起来像个反向断言。

    我最初写的 `events_from_run_dir` 会从每一步的 `guardrail_decisions` 里
    "反推"出 `guardrail_blocked` / `approval_resolved` 事件。看起来白捡，
    但它让回放成了"当时发生了什么"的**第二份构造**：
    实时的那份由拦截器在判定那一刻发出，回放的这份由 `decision == "block"`
    反推 —— 两条规则一旦不一致，回放会**安静地**显示出一段与操作员
    当时所见不同的历史。

    改成不合成之后，两个通道的分工是一句话：
      · 独立的事件类型只为【延迟】存在，不为【信息】存在 ——
        它们的载荷在每一步的 `guardrail_decisions` 里已经有了。
      · 回放只需要 run_started / step_completed / run_completed。
    前端在两条路上读的是**同一个字段**，没有第二份推断。

    ★ 所以这里断言的是"回放里没有这两类事件"，而它必须**先证明素材存在**
      （这一步的判定里确实有 block、也确实有一次被拒的审批）——
      少了前半句，一个"压根没读到任何 step"的实现也能通过这条用例。
    """
    step = {
        "run_id": "r1", "task_id": "t1", "attempt": 1, "step": 2,
        "actions": [{"name": "guard_notice", "params": {}, "element_text": None}],
        "results": [],
        "guardrail_decisions": [
            {"rule_id": "block-destructive", "decision": "block", "reason": "破坏性",
             "matched_rule_ids": ["block-destructive"]},
            {"rule_id": "confirm-edit", "decision": "confirm", "reason": "写入",
             "matched_rule_ids": ["confirm-edit"], "approved": False, "approved_by": "web"},
        ],
    }
    _write_run(tmp_path / "r1", steps=[step])
    events = events_from_run_dir(tmp_path / "r1")

    # 对照实验的前半句：素材确实在（有 block 判定、也有一次被拒的审批）。
    decisions = [d for e in events for d in e.data.get("guardrail_decisions", [])]
    assert any(d["decision"] == "block" for d in decisions), "素材里没有 block 判定"
    assert any(d.get("approved") is False for d in decisions), "素材里没有被拒的审批"

    types = [e.type for e in events]
    assert types == ["run_started", "step_completed", "run_completed"], (
        f"回放合成出了额外的事件类型：{types}\n"
        f"  拦截/审批结论必须从每一步的 guardrail_decisions 读，"
        f"而不是在这里反推一份 —— 反推出来的那份会和实时的那份漂移。"
    )


def test_replay_seq_is_contiguous_and_starts_at_one(tmp_path: Path):
    """★ 回放的事件也编 seq，虽然它们从未经过总线。

    理由：SSE 的 `id:` 字段在两个通道里都该有值。回放全是 0 的话，
    前端就得知道"这次别信 id" —— 那是第二套逻辑，正是本模块要避免的。
    """
    _write_run(tmp_path / "r1", steps=[{"step": 1}, {"step": 2}, {"step": 3}])
    events = events_from_run_dir(tmp_path / "r1")
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


def test_replay_marks_itself_so_the_ui_can_tell(tmp_path: Path):
    """★ `replay: True` 是实时通道**没有**的字段 —— 前端只读它决定一个徽标。

    它不是渲染需要的（所以"一个渲染器"没被破坏），但它是必须的：
    "这是刚跑完的"和"这是三天前的历史"在看板上不该长得一样。
    """
    _write_run(tmp_path / "r1", steps=[{"step": 1}])
    events = events_from_run_dir(tmp_path / "r1")
    last = events[-1]
    assert last.type == "run_completed"
    assert last.data["replay"] is True


def test_replay_carries_finished_at_in_the_payload_not_just_as_the_timestamp(tmp_path: Path):
    """★★ 回放的 `run_completed` 必须在**载荷里**带 `finished_at`。

    ★ 这条的来历是一个"前端读了、生产者从来没给过"的死字段：
      看板收到 `run_completed` 会拼一行 `"\\n结束 " + (d.finished_at || "")`，
      而**两个通道都没有发过这个字段** —— 于是那行 100% 渲染成"结束 "后面空着。
      它不会报错、不会留日志，只会让每次演示都少一个信息且没人察觉。

      回放这边尤其冤：`finished_at` 明明已经**用作这条事件的时间戳**了
      （上面 `_emit` 的第二个参数），只是没放进载荷。

    ★ 断言写成**与 run.json 里的值相等**，不是"字段存在"：
      `"finished_at" in data` 在它恒为 `""` 时也会通过 —— 那就等于没测。
    """
    _write_run(tmp_path / "r1", steps=[{"step": 1}])
    last = events_from_run_dir(tmp_path / "r1")[-1]
    assert last.type == "run_completed"
    assert last.data["finished_at"] == "2026-09-17T00:00:12+00:00", (
        f"回放载荷里的结束时刻是 {last.data.get('finished_at')!r}，"
        f"而 run.json 里写的是 2026-09-17T00:00:12+00:00"
    )
    # ★ 与事件自身的时间戳一致：同一个事实不该有第二个来源，写歪了就是分叉。
    assert last.at == last.data["finished_at"]
    # ★ 实时那一半在 tests/test_mock_pdd_e2e.py（真要跑一次 run 才发得出来）——
    #   两条是一对：只钉回放的话，实时通道把字段删掉仍然全绿。


def test_replay_run_started_carries_the_whitelist_from_the_run_snapshot(tmp_path: Path):
    """★★ 回放的 `run_started` 要带 `allowed_domains`，且值来自**这次 run 的策略快照**。

    ★ 这条和上一条是同一类缺陷的第二次出现：看板读一个字段，而某个通道从没发过它。
      这次是**回放**缺 —— 实时有（`runner.py` 发的是 `spec.guardrails.allowed_domains`），
      回放没有，前端 `|| []` 一兜，界面上就印出「白名单 []」。
      它比空白坏得多：空白让人怀疑，`[]` 读起来是一个结论 ——
      "这次 run 没有域名白名单"，而这恰恰是一句**假话**，是护栏叙事里最不该出错的地方。

    ★ 断言写成**与 run.json 里的值相等**，不是"字段存在"：
      `[] == []` 会让一个恒发空数组的实现通过，那正好是这条要防的那个 bug。

    ★ 值必须取自 run.json 的 `guardrail_policy`（那一次的**策略快照**），
      不是当前的配置 —— 否则跑完改了配置再回放，看到的是"现在管着什么"，
      而不是"当时管着它的是什么"。所以这里专门写一个**与当前配置不同**的白名单，
      取值来源写错时（比如读 config / 读默认值）会立刻显形。
    """
    record = {
        "run_id": "r1",
        "task_id": "t1",
        "task_name": "任务",
        "started_at": "2026-09-17T00:00:00+00:00",
        "finished_at": "2026-09-17T00:00:12+00:00",
        # ★ 完整的 GuardrailSpec 快照长这样（runner.py 存的是 model_dump(mode="json")），
        #   这里只留断言用到的那部分 —— 多写的字段不影响这条用例要钉的事实。
        "guardrail_policy": {
            "allowed_domains": ["mms.pinduoduo.com", "*.pinduoduo.com"],
            "prohibited_domains": ["*.taobao.com"],
            "default_decision": "confirm",
        },
    }
    _write_run(tmp_path / "r1", steps=[{"step": 1}], record=record)

    first = events_from_run_dir(tmp_path / "r1")[0]
    assert first.type == "run_started"
    assert first.data["allowed_domains"] == ["mms.pinduoduo.com", "*.pinduoduo.com"], (
        f"回放首帧的白名单是 {first.data.get('allowed_domains')!r}，"
        f"而 run.json 的策略快照里写的是 ['mms.pinduoduo.com', '*.pinduoduo.com']\n"
        f"  前端会把缺字段兜成 []，于是看板上显示「白名单 []」—— 一句很确定的假话。"
    )


def test_replay_run_completed_says_how_many_attempts_were_used(tmp_path: Path):
    """★ 看板那行"· N 次尝试"读的是 `attempts` —— 回放原先没这个键，永远显示"?"。

    ★ 值取自 run.json 的 `attempt`：runner 写记录时是 `attempt=self.attempts`，
      也就是"用掉了几次"，和实时发的 `attempts` 是同一个数。
      断言写成与 run.json 的值相等，理由同上面两条：只断言"键存在"的话，
      一个恒发 0 的实现照样通过，而那正好会让 UI 退回显示"?"。
    """
    record = {
        "run_id": "r1",
        "started_at": "2026-09-17T00:00:00+00:00",
        "finished_at": "2026-09-17T00:00:12+00:00",
        "attempt": 2,   # ← 第二次才成功的那种 run
    }
    _write_run(tmp_path / "r1", steps=[{"step": 1}], record=record)
    last = events_from_run_dir(tmp_path / "r1")[-1]
    assert last.type == "run_completed"
    assert last.data["attempts"] == 2, (
        f"回放载荷里的尝试次数是 {last.data.get('attempts')!r}，run.json 里写的是 2"
    )


def test_replay_run_completed_carries_the_observed_login_state(tmp_path: Path):
    """★★ 回放的 `run_completed` 必须带**实测**登录态 —— 否则看板的回放视图
    会把"这次其实是被登录页挡下的"渲染成一次平平无奇的 `completed / empty / 0 行`。

    ★ 为什么这一条比前面几条（`finished_at` / `attempts`）更要紧：
      那几条丢的是一行字，这一条丢的是一个**结论**。零行 run 的两个成因
      （店里真没数据 / 登录态失效）在产物里本来长得一模一样，
      `login_state` 是唯一能分开它们的字段 —— 而回放通道恰好是**看历史**的那条，
      也就是人最可能去翻"上次为什么是零行"的地方。它在回放里丢掉最讽刺。

    ★ 断言写成**与 run.json 里的值相等**，理由同前：`"login_state" in data`
      在一个恒发 `""` 的实现上也通过，而那正好是 UI 退回"什么线索都没有"的形态。
    """
    record = {
        "run_id": "r1",
        "started_at": "2026-09-17T00:00:00+00:00",
        "finished_at": "2026-09-17T00:00:12+00:00",
        "login_state": "login_page",
        "login_state_reason": "URL 命中登录页特征：/login",
    }
    _write_run(tmp_path / "r1", steps=[{"step": 1}], record=record)
    last = events_from_run_dir(tmp_path / "r1")[-1]
    assert last.type == "run_completed"
    assert last.data["login_state"] == "login_page", (
        f"回放载荷里的登录态是 {last.data.get('login_state')!r}，而 run.json 里写的是 login_page"
    )
    assert last.data["login_state_reason"] == "URL 命中登录页特征：/login"


def test_replay_of_a_run_that_never_probed_says_so_instead_of_guessing(tmp_path: Path):
    """★ 对照：**Phase 6 之前**的 run.json 里根本没有 `login_state` 这两个键。

    它们必须回放成 `""`（=【没探过】），而不是被填成某个看起来正常的判决。
    ⚠️ 具体说：**不能**退化成 `"logged_in"`。反了的话，看板会给一批
    "登录态失效、而当时根本没人知道"的历史 run 盖上"已登录"的章，
    而这正好让人**不去**看那几张截图 —— 比不显示还坏。

    ★ 手法还是本项目的老手法：先钉空值本身，再钉这个空值**不等于**任何一个真判决。
      只断言前者的话，一个"缺键就填 logged_in"的实现照样通过 —— 它也有键、也有值。
    """
    _write_run(tmp_path / "r1", steps=[{"step": 1}])  # ← 默认 record 就是老 run.json 的形状
    last = events_from_run_dir(tmp_path / "r1")[-1]
    assert last.data["login_state"] == ""
    assert last.data["login_state_reason"] == ""
    # ★ 空值必须**区别于**三个真判决：两个能让人立刻采取行动，一个（unknown）要人去看图，
    #   而 "" 的处置是"这个任务压根不需要登录态、别多想"。
    assert last.data["login_state"] not in ("logged_in", "login_page", "unknown")


def test_replay_of_a_crashed_run_still_produces_a_start(tmp_path: Path):
    """★ run 起了但没写 run.json（进程被杀）时，回放**不能崩**、也不能空手而回。

    这种目录是一个真实存在、且有诊断价值的事实（"run 起来了但没走完"）。
    回放崩掉的话，看板只能显示一个 500 —— 而那个目录里明明有 steps.jsonl，
    也就是"它跑到哪儿了"这个最该看的信息其实是在的。

    ★ 同时钉住：**不能凭空造一个 run_completed**。给一次没跑完的 run 盖上
      "已完成"，是比 500 更坏的结果。
    """
    run_dir = tmp_path / "half"
    run_dir.mkdir()
    (run_dir / "steps.jsonl").write_text(
        json.dumps({"step": 1, "actions": []}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    events = events_from_run_dir(run_dir)
    types = [e.type for e in events]
    assert types == ["run_started", "step_completed"], (
        f"没有 run.json 时的事件序列是 {types}；"
        f"预期只有开始 + 已有的步（不该凭空造一个 run_completed）"
    )
    # ★ 而且 run_id 要退到目录名 —— 不然前端拿不到一个可用的 id。
    assert events[0].run_id == "half"


def test_replay_of_a_run_that_never_existed_produces_nothing(tmp_path: Path):
    """★★ 目录**根本不存在** → 一条事件都不发。与上一条是一对，别合并。

    ★ 上一条说的是「目录在、run.json 不在」（崩溃的 run，目录本身就是
      "它真的开始过"的证据）→ 照样产事件。
      这一条说的是「目录都不在」（id 是错的 / 链接过期 / run 被清理了）→
      **不能**凭空造一条 run_started 出来。

    ★★ 这个区分是被实测逼出来的：以前这里会产出**一条空的 run_started**
      （task_id 是空串、run_id 取自目录名），前端于是显示出一个
      「开始过、但一步都没跑」的 run —— 一个**看起来像事实的伪造**。
      排查的人会去查"这次 run 为什么没跑起来"（查错方向），
      而真相是"这个 id 不存在"。看板对这个事件没有任何可怀疑之处：
      run_started 本来就该是第一条。

    ★ 为什么这条判在**函数**这一层，而不是只靠 webapp 那条 404：
      404 是策略（"这个条件回什么状态码"），这里是事实（"这个目录里有什么"）。
      只判前者的话，将来有人给 events_from_run_dir 换一个调用方
      （报告、CLI 回放、导出工具），伪造就会从新入口漏出去 ——
      而那些调用方不会去查 webapp 里的 is_dir。
    """
    events = events_from_run_dir(tmp_path / "does-not-exist")
    assert events == [], (
        f"一个不存在的 run 目录产出了 {[e.type for e in events]} —— "
        f"预期一条都没有。造出来的事件会在看板上冒充事实。"
    )
