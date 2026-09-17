"""运行期事件 —— run 进行中对外说话的那条通道（Phase 5 的 SSE 底座）。

★★ 这个模块存在的理由，是 run 的**两个读者**需求相反：

    · **落盘**（`steps.jsonl` / `run.json`）要的是**完整、可审计、崩溃不丢**，
      所以它是追加写、一步一行，而且只能在一步**结束之后**才写得出来；
    · **看板**要的是**现在在干嘛**，它必须在一件事**发生的当下**就知道，
      而且要能在浏览器里逐条滚出来。

  在一张盘上同时满足这两件事是做不到的（磁盘没有"还没发生但快发生了"）。
  所以事件是一条**独立的**通道 —— 但它的载荷**就是**那些记录本身，
  不是另抄一份摘要（见下）。

★★ 本模块最重要的一条设计约束，是「回放不是另做一套渲染」：

    实时推送的第 3 步，和三天后从盘上回放出来的第 3 步，
    必须是**同一个形状**，于是前端只有**一个**渲染器。

  所以 `step_completed` 的载荷就是 `StepRecord.model_dump()` —— 也就是
  `steps.jsonl` 里的那一行，逐字节同源。`events_from_run_dir()` 干的事
  只是把盘上的行重新包成事件，**不做任何再解释**。
  一旦这里出现"实时推一份、回放读另一份"的两套构造，两份就会开始漂移，
  而漂移的表现是"回放看到的历史和当时看到的不一样" —— 没人会立刻发现。

★ 为什么事件只有一个 `type` + 一个 `data: dict`，而不是每个类型一个 pydantic 模型：
  那样 `step_completed` 就得再定义一个 `StepEventPayload`，和 `StepRecord`
  重复一遍字段 —— 于是"同一个事实"有了两个定义，改一个忘一个。
  代价是 `data` 的形状在类型层面宽了。**这个代价用一处校验补回来**：
  `type` 是 `Literal`，写错的事件名在**构造那一刻**就炸（见 `RunEvent.type`）。
  形状的契约写在每个发布点的注释里，并在 `tests/test_events.py` 里逐条钉住。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from ecom_agent.observability.models import now_iso

QUEUE_MAX = 512
"""订阅者队列上限。

★ 位置在 `EventBus` **之前**不是排版选择：它被用作 `__init__` 的默认参数，
  而默认参数在**函数定义那一刻**求值 —— 也就是类体执行时。
  写在类后面的话，模块一导入就 `NameError: QUEUE_MAX`，
  而且报错指向的是 `def __init__` 那一行，看起来像类写错了。

★ 取值理由：按事件大小（一步约 1–3 KB）算，512 条约 1 MB ——
  对一个浏览器标签页绰绰有余。它存在的主要目的是**兜住"客户端卡死"**：
  没有上限的话，一个不读连接的客户端会让服务端内存跟着 run 的步数一路涨。
"""

EventType = Literal[
    "run_started",
    "step_completed",
    "guardrail_blocked",
    "approval_required",
    "approval_resolved",
    "run_completed",
    "error",
    "stream_gap",
]
"""契约里的事件名。★ 用 Literal 而不是 str：pydantic 会在构造时拒绝未知名字。

  这条强制点针对的是一个真实的失败形态：发布点拼错事件名（`step_complete`），
  前端 `addEventListener` 挂的却是正确名字 —— 于是**那类事件一条都不显示**，
  而 SSE 连接、HTTP 状态码、后端日志全都正常。
  拼错在这里炸掉，比在浏览器里静默消失好得多。
"""


class RunEvent(BaseModel):
    """一条运行期事件。"""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    run_id: str = ""
    seq: int = 0
    """从 1 开始的单调序号，**由 EventBus 分配**（-1 表示还没入总线）。

    ★ 它同时干三件事，所以不是可有可无的装饰：
      1. SSE 的 `id:` 字段 —— 断线重连时浏览器会带 `Last-Event-ID` 回来；
      2. 前端**检测丢事件**：`seq` 不连续就说明总线丢过（见 `stream_gap`）；
      3. 回放时对齐"实时看到的"和"盘上读到的"。
    """

    at: str = Field(default_factory=now_iso)
    data: dict[str, Any] = Field(default_factory=dict)

    def sse(self) -> str:
        """一帧 SSE。

        ★ 三个字段各有用途，缺一个都会退化成"能收但没法管"：
          · `id:`   —— 断线重连的续传锚点；
          · `event:` —— 前端按类型 `addEventListener`，而不是自己 switch；
          · `data:` —— 必须是**单行**，SSE 的帧分隔符就是空行。

        ★ `ensure_ascii=False` + 不能出现裸换行：这两件事不矛盾 ——
          `json.dumps` 会把字符串里的换行转义成 `\\n` 两个字符，
          所以中文可以原样出去（页面上的元素文本是人读的），
          而帧结构不会被内容里的换行破坏。
          反过来写成 `ensure_ascii=True` 的话，页面上全是 \\uXXXX，
          "让看板显示人话"这件事就白做了。
        """
        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"


class _Subscriber:
    """一个订阅者：一条有界队列 + 一个"我漏过东西"的计数。

    ★★ 为什么"漏了"记在这里，而不是往队列里塞一条通知 —— 这是本模块
      唯一一处**写完发现自己写错了**的地方，值得完整记下来。

      第一版是"尝试往队列里插一条 `stream_gap`，插不进去就算了"，
      注释里还写了理由："队列满说明它连这条通知都收不下，那再塞只会把队列搅乱"。
      看起来是个合理的降级设计。但它有一个结构性的问题：

        **调用点在 `except QueueFull` 里面，而那里队列必然是满的** ——
        我们刚刚丢掉最旧的一条、塞进最新的一条，队列**又是满的**。
        也就是说"还有一个空位"这个分支**永远不可达**，
        `stream_gap` 从来没有真正发出去过一条。

      ★ 这类错误的形态值得单独记一句：**注释描述了一种代码里不存在的可能性**。
        读注释的人（包括三天后的我）会以为"通知发不出去"是罕见的边界情况，
        而实际上它是**全部**情况。而且它不会以任何方式报错 ——
        前端那条 `case "stream_gap"` 只是静静地从不执行。

      ★ 改成记在订阅者身上之后有两个好处：
        1. 通知**不可能因为没地方而发不出去**（它不占队列位置）；
        2. N 次丢弃**合并成一条**通知，而不是每丢一条插一条 ——
           慢客户端最不需要的就是一堆"你好慢"的通知。

      代价是通知的时机从"丢弃那一刻"变成"订阅者下次来读的时候"，
      而后者其实更对：订阅者没在读的时候，通知给它也没用。
    """

    __slots__ = ("queue", "missed")

    def __init__(self, maxsize: int) -> None:
        # ★ 类型标注写在 __slots__ 之外（赋值处），因为 __slots__ 里不能带类型。
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.missed = 0
        """因为跟得太慢而被丢给**这一个**订阅者的事件数。★ 与总线的 `dropped`
        是两回事：另一个订阅者可能完全跟得上，不该替它背这个数。"""


class EventBus:
    """一次 run 的事件总线：一对多，且**记得住过去**。

    ★ 为什么必须留 backlog（而不是"接上之后只推新的"）：
      看板页面会刷新、会换标签页、会因为 SSE 断线重连。只推新事件的话，
      刷新之后**前面发生过什么就永远看不到了** —— 而看板最常被打开的时刻，
      恰恰是"刚才那步好像不对，我刷新看看"。
      留了 backlog，新订阅者先补历史再续实时，刷新变成一个无害动作。

    ★★ `publish` 是**同步**的，这是刻意的，不是为了省一个 async：
      总线要在"把事件追加进 backlog"和"投进各个订阅者队列"之间**不能有 await**，
      否则与 `subscribe()` 的交错会让事件**重复或丢失**（订阅者先拿到 backlog
      快照、后进订阅集合，中间发布的那条就两头都没有）。
      同步执行让这两步成为一段不可分割的代码，正确性靠"没有 await"保证，
      而不是靠加锁 —— 也就没有锁可以忘记释放。

      副作用是它也**不会在拦截器的热路径上引入一个挂起点**：护栏判定里
      `await` 一次总线，意味着"浏览器那边的东西"有机会在判定中间插进来。
    """

    def __init__(self, *, run_id: str = "", queue_max: int = QUEUE_MAX) -> None:
        self.run_id = run_id
        self.queue_max = queue_max
        self._events: list[RunEvent] = []
        self._subscribers: set[_Subscriber] = set()
        self._closed = False
        self.dropped = 0
        """★ 因为某个订阅者跟得太慢而丢掉的事件数。**这是要显示的，不是内部细节。**
        一个"悄悄丢事件的看板"和"实时看板"在页面上长得一模一样。

        ★ 按"订阅者×事件"计数：同一个事件让两个订阅者各丢一次，这里就是 2。
          它是"观众体验的总损失量"，不是"丢了几个事实"。"""

    # ── 发布 ──────────────────────────────────────────────
    def publish(self, event: RunEvent) -> RunEvent:
        """入总线。返回带 seq 的那一条（调用方通常不看返回值）。"""
        if self._closed:
            # ★ 关闭之后的事件直接丢，且**不报错**：run 结束与收尾事件之间
            #   天然存在竞态（比如 finally 里的清理日志），为它抛异常会让
            #   一次正常的收尾变成失败。丢掉的这段时间本来也不该有新事实。
            return event
        event = event.model_copy(
            update={"seq": len(self._events) + 1, "run_id": event.run_id or self.run_id}
        )
        self._events.append(event)
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # ★ 丢**最旧的**而不是最新的一条，并且只丢给这一个订阅者。
                #   为什么丢最旧：慢客户端最需要的是"现在在干嘛"，
                #   而它已经错过的那些可以从盘上补（backlog / 回放接口）。
                #   为什么只丢给这一个：另一个订阅者可能跟得上，
                #   不该因为别人慢而少看内容。
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                # ★ 记在订阅者身上，**不**往队列里塞通知 —— 理由见 _Subscriber
                #   的 docstring（那里队列必然是满的，塞不进去）。
                sub.missed += 1
                self.dropped += 1
        return event

    def _gap_event(self, sub: _Subscriber, missed: int) -> RunEvent:
        """给这个订阅者的"你漏了东西，去盘上补"通知。"""
        return RunEvent(
            type="stream_gap",
            run_id=self.run_id,
            data={
                "dropped_total": self.dropped,
                "dropped_to_this_subscriber": missed,
                "hint": "订阅者太慢，已丢弃部分事件；请改用 GET /api/runs/{id} 取完整回放",
            },
        )

    def close(self) -> None:
        """宣告"不会再有新事件了"。

        ★ 不能只把订阅者丢掉：那样它们的 `await q.get()` 会永远挂着，
          对应的 SSE 连接就一直不关 —— 表现为浏览器标签页转圈，
          而服务端认为一切正常。
        """
        self._closed = True
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(_CLOSE)
            except asyncio.QueueFull:
                # 队列满也要能关：清一个位置出来塞关闭哨兵。
                # 这条路径上"少一条历史事件"远好于"连接永远不关"。
                # ⚠️ queue_max=1 时这一步会挤掉唯一的那条事件 ——
                #    只有一个位置时"保留最新的"和"塞进哨兵"不可兼得，
                #    这里选后者。漏掉的那条由 seq 不连续兜底（前端会去补全量）。
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(_CLOSE)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ── 订阅 ──────────────────────────────────────────────
    async def subscribe(self) -> AsyncIterator[RunEvent]:
        """先补 backlog，再续实时。"""
        sub = _Subscriber(self.queue_max)
        # ★★ 顺序不能反：**先**进订阅集合，**再**取 backlog 快照。
        #   反过来的话，两步之间（虽然当前实现里没有 await）发布的事件
        #   会既不在快照里、也没被投递 —— 一条事件无声消失。
        #   而且这个顺序是**安全的**：先订阅不会造成重复，因为 backlog
        #   快照取的是"此刻已有的"，发布进去的那条只有在快照之后才入队，
        #   两者不会同时包含它。
        self._subscribers.add(sub)
        backlog = list(self._events)
        try:
            for event in backlog:
                yield event
            if self._closed:
                return
            while True:
                item = await sub.queue.get()
                # ★★ 缺口通知在**这里**发 —— 订阅者真的来读的时候。
                #   除了"不占队列位置"，这个时机还多覆盖一种情况：
                #   队列被塞满之后 run 就结束了，此时下面是 _CLOSE 分支，
                #   而如果不在这里补发通知，这个订阅者会**一路读到连接关闭、
                #   却永远不知道自己漏了几十条** —— 那正是"看板悄悄少显示
                #   一段历史"这个最不能出的错。
                if sub.missed:
                    missed, sub.missed = sub.missed, 0
                    yield self._gap_event(sub, missed)
                if item is _CLOSE:
                    return
                yield item
        finally:
            self._subscribers.discard(sub)

    def backlog(self) -> list[RunEvent]:
        return list(self._events)

    @property
    def closed(self) -> bool:
        return self._closed


# ★ 关闭哨兵。用一个模块级单例而不是 None：None 是合法的队列元素语义之外的
#   值，但用一个专用对象能让"这是关闭信号"在读代码时一眼可辨，
#   也不会和"某天有人往队列里塞 None"混淆。
_CLOSE = object()


# ── 回放：从盘上重建同形状的事件 ──────────────────────────
def events_from_run_dir(run_dir: Path | str) -> list[RunEvent]:
    """把一次跑完的 run 重新包成事件流。**不做任何再解释。**

    ★★ 这是"回放不是另做一套渲染"的落地处：产出的 `step_completed`
      载荷与实时推的**逐字节同源**（都是 `StepRecord.model_dump()`），
      所以前端那一个渲染器对两条路都成立。

    ★★★ 这里**刻意不合成** `guardrail_blocked` / `approval_*` 事件 —— 这是
      本函数最容易写错、也最值得说明的一处决定。

      一开始我写的就是"从每一步的 `guardrail_decisions` 里推出拦截/审批事件"，
      看起来很自然（信息都在），但它有一个致命的性质：
      **它成了"当时发生了什么"的第二份构造**。
      实时的拦截事件由拦截器在判定那一刻发出，回放的拦截事件由这里按
      "decision == 'block'" 反推 —— 两条规则一旦哪天不一致
      （比如拦截器改了、或者新增了一种 decision 字面量），
      回放就会**安静地**显示出一段与操作员当时所见不同的历史。
      而"历史回放和当时看到的不一样"是审计场景里最不能出的错。

      改成不合成之后，两个通道的分工变成一句话：

        · **独立的事件类型只为【延迟】存在，不为【信息】存在。**
          它们的载荷是 `StepRecord` 里已经有的东西（`guardrail_decisions`
          就在每一步的载荷里）；实时多发一条，只是为了让看板**当场**有反应
          （弹卡片、打标记），而不是等这一步记完。
        · **回放只需要 run_started / step_completed / run_completed**，
          拦截与审批状态从每一步载荷里的 `guardrail_decisions` 直接读。
          于是前端在两条路上读的是**同一个字段**，没有第二份推断。

      → 换来的性质是：**回放不可能与实时不一致**，因为它压根不解释任何东西。

    ★ 诚实标注两件回放**补不出来**的事（它们只存在于实时通道）：
      1. **硬停那一步的判定**：被中止的步不产生历史项，`steps.jsonl` 里没有
         它的行，判定也就没跟着落盘（runner 的 `_make_step_hook` 在
         "历史没变长"时明确丢弃它们 —— 那段注释解释了为什么没有地方可挂）。
         实时看板上看得到，回放里看不到。
      2. **过程时序**：`approval_required` 与它的 `approval_resolved` 之间
         隔了多久，盘上没有到那个粒度的记录（`decided_at` 只有决定时刻，
         而"什么时候开始等的"在 `pending/*.json` 里，那份文件决策后就被移走了）。
      这两条都不是 bug，是"落盘记录的是**事实**，不是事实发生的**过程**"。
      之所以写在这里而不是含糊过去：回放看起来越完整，人越会以为它是全量。
    """
    run_dir = Path(run_dir)
    out: list[RunEvent] = []

    # ★★ 目录不在 → **一条事件都不发**，而不是发一条空的 run_started。
    #
    #    这条分支补的是一个真的漏洞：以前这里直接往下走，于是 run.json 和
    #    steps.jsonl 都读不到时，产出的是**一条 run_started**（task_id 是空串，
    #    run_id 取自目录名）—— 看板上显示出一个"开始过、但什么都没有"的 run，
    #    而真相是**这个 run id 根本不存在**（链接过期、id 手输错、run 被清理了）。
    #
    #    造出来的那条事件为什么特别坏：**它长得像一个事实**。看板对它没有任何
    #    可怀疑之处（run_started 本来就该是第一条），于是人会去查"这次 run 为什么
    #    一步都没跑" —— 而该做的是"这个 id 是错的"。
    #    这属于本项目反复警惕的同一类问题：**看起来有记录，实际没发生过。**
    #
    #    ⚠️ 和"目录在、但 run.json 不在"要分清：那是崩溃/中断的 run，
    #       目录本身就是"它真的开始过"的证据（webapp 还会把它标成 incomplete）。
    #       那条路径照旧产事件，别把这里写成"读不到 run.json 就返回空"。
    if not run_dir.is_dir():
        return out

    run_json = run_dir / "run.json"
    record: dict[str, Any] = {}
    if run_json.is_file():
        record = json.loads(run_json.read_text(encoding="utf-8"))

    def _emit(type_: str, at: str, data: dict[str, Any]) -> None:
        # ★ seq 在这里也编上，虽然这些事件从未经过总线。
        #   理由：SSE 的 `id:` 字段在两个通道里都该有值 —— 实时通道的 id 来自
        #   总线，回放通道要是全是 0，那么"同一个渲染器"在客户端就得知道
        #   "这次别信 id"。那是第二套逻辑，正是本模块开头要避免的东西。
        #   编号从 1 开始且连续，与总线分配规则一致。
        out.append(
            RunEvent(
                type=type_, run_id=record.get("run_id", "") or run_dir.name,
                seq=len(out) + 1, at=at or now_iso(), data=data,
            )
        )

    _emit(
        "run_started",
        str(record.get("started_at", "")),
        {
            "run_id": record.get("run_id", run_dir.name),
            "task_id": record.get("task_id", ""),
            "task_name": record.get("task_name", ""),
            "params": record.get("params", {}),
            "start_url": record.get("start_url", ""),
            "task_fingerprint": record.get("task_fingerprint", ""),
            # ★ 白名单要从 run.json 的**策略快照**里取，不是重新算一遍当前的策略。
            #   看板那行"白名单 [...]"读的就是它 —— 不回放的话前端拿到 undefined，
            #   `|| []` 兜成空数组，于是界面上显示的是「白名单 []」：
            #   一句**看起来很确定、但是假的**话（这次 run 明明带着白名单跑的）。
            #   空的方括号比空白坏得多，因为它读起来像一个结论。
            #
            #   为什么取自 `guardrail_policy` 而不是配置：策略快照是**那次 run 的**
            #   策略（models.py 里写了理由）—— 回放时必须回答"当时管着它的是什么"，
            #   而不是"现在管着什么"。
            "allowed_domains": (record.get("guardrail_policy") or {}).get("allowed_domains", []),
        },
    )

    steps_path = run_dir / "steps.jsonl"
    if steps_path.is_file():
        for line in steps_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            step = json.loads(line)
            _emit("step_completed", str(step.get("started_at", "")), step)

    if record:
        _emit(
            "run_completed",
            str(record.get("finished_at", "")),
            {
                "run_id": record.get("run_id", ""),
                "status": record.get("status", ""),
                "parse_status": record.get("parse_status", ""),
                "rows_collected": record.get("rows_collected", 0),
                "steps": record.get("steps", 0),
                "duration_s": record.get("duration_s", 0.0),
                # ★ 和实时通道**逐字段对齐**：这个值上面已经用作事件时间戳了，
                #   但没放进载荷 —— 于是看板那行"结束 …"在回放里也永远是空的。
                #   "实时和回放载荷形状一样"这条约束，靠的就是这类字段两边都发。
                "finished_at": str(record.get("finished_at", "")),
                # ★ 同一类问题的第三次：看板那行"· N 次尝试"读的是 `attempts`，
                #   实时发了、回放没发 → 回放永远显示"· ? 次尝试"。
                #   这次不比白名单那次严重（"?" 是**承认不知道**，不是假话），
                #   但两边的形状不一致，前端迟早要为此分叉。
                #
                #   ★ 取 `attempt` 而不是另存一个 `attempts`：run.json 里那个字段
                #     就是 runner 的 `self.attempts`（写记录时 attempt=self.attempts），
                #     也就是"用掉了几次"本身 —— 不是"第几次"。再存一个同义的字段
                #     只会多一处能写歪的地方。
                #   ★ 缺这个键时给 0：前端是 `d.attempts || "?"`，0 是 falsy，
                #     于是老 run（没有这个键）显示"?"，而不是一句假的"0 次尝试"。
                "attempts": record.get("attempt", 0),
                "errors": record.get("errors", []),
                # ★ `replay: True` 是实时通道**没有**的一个字段，刻意留着：
                #   它不是渲染需要的，是给"看板该不该显示实时状态灯"用的。
                #   前端只读它决定一个徽标，不参与步骤渲染 ——
                #   所以"一个渲染器"这条约束没有被破坏。
                "replay": True,
            },
        )
    return out
