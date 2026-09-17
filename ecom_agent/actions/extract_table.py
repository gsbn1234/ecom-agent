"""`extract_table` —— 确定性地把页面上的表格读成结构化行。

★★ 为什么库已经有内置 `extract` 了，还要自己写一个：

  browser-use 的内置 `extract`（`tools/service.py:1071` 起）是**经过 LLM 的**：
  它把页面内容交给 `page_extraction_llm` 再产出一段内容。对
  "这个商品卖多少钱、还剩几件"这类问题，那条路的失败模式是**抄错** ——
  模型把 59.90 写成 59.00、把"近30天销量"填进"库存"，而产出的格式完全合法。
  pydantic 校验不出任何问题，报告里一片正常，sanity_flags 也未必拦得住
  （59.00 是个完全合理的价格）。

  一个卖家可能拿着这个数去补货。所以价格、库存这类字段不该由模型来转录。

  `extract_table` 走另一条路：**一个 LLM 都不经过**，直接把 DOM 里的单元格
  文本原样读出来。它的失败模式是"读不到"（没找到表格 → 返回 error），
  而那是一种**看得见的失败** —— 会进 steps.jsonl、会累加连续失败、会让人去看。

  ★ 这不是"不要 LLM"，是分工：
      LLM 负责**决定读哪个表、什么时候读**（语义理解，它擅长）；
      `extract_table` 负责**把值抄下来**（逐字符转录，它不擅长）。
    把每一步交给能做对它的那一方，而不是让一个模型从头包到尾。

★★ 与结构化输出（`done` + ProductRowList）的关系，先说清楚免得被问住：

  本项目的入库路径仍然是 LLM 产出 `done(rows=[...])`。那它白做吗？不。
  它产出的是**可对照的原文**：
    · 同一份字节既进了 `steps.jsonl`（审计日志），又给了 LLM 去抄；
    · 于是"页面当时到底写了什么"和"最后入库的是什么"可以**逐字段 diff**。
  没有它的话，"LLM 抄错了"在事后是**不可证伪的** —— 页面早就翻页或关掉了，
  谁也说不清当时那一格是 59.90 还是 59.00。
  也就是说：它把"模型出错"从"没人知道"变成"一条可以执行的查询"。

★★ 签名形状是被库的注册表契约决定的，不是随便写的：

  `Registry._normalize_action_function_signature` 把 action 归一化成
  「只收 kwargs」，然后**按位置**调用原函数（`registry/service.py:395` 起）：

      call_args.append(params)                       # Type 1：装饰器显式给了 param_model
      call_args.append(kwargs['browser_session'])    # 特殊参数由库注入

  所以签名必须是 `(params: ExtractTableAction, browser_session: BrowserSession)`，
  且注册时必须带 `param_model=ExtractTableAction`。三种写错的方式各有各的表现：
    · 不给 `param_model` → 走 Type 2，`params` 本身被当成一个 action 字段
      **暴露给 LLM**（schema 里出现一个永远填不对的 params 对象）；
    · 函数里写 `**kwargs` → 注册时抛 ValueError（库明确禁止，见归一化的第 1 步）；
    · `browser_session` 注解写错类型 → 抛
      "conflicts with special argument injected by tools"，
      那句话读起来像"参数重名"，实际是类型不兼容。
  三种都在注册期就炸（不是运行时），这是好事；但报错都指不到真正的原因，所以记在这里。

★★ 为什么 **不**给这个 action 配 `domains=`：

  库的 `domains` 不是运行时拦截，只是"按 URL 条件决定要不要把这个 action
  暴露给 LLM"（`tools/registry/views.py:128-154`，见 docs/ADR.md 第 6 条）。
  配了域名的收益是"在别的站上 LLM 看不到这个动作"，代价是**看不见的失效**：
  哪天换平台、或者 mock 换了个 host，这个动作就从 schema 里静默消失，
  表现为"LLM 死活不调 extract_table"。
  而它本来就是只读的 —— 给它加域名限制是安全表演，不是安全。
  真正需要拦的是 `click`/`input` 那类会改数据的动作，而那是内置的，加不了。
★★★ 本文件**绝对不能**写 `from __future__ import annotations`（实测，不是风格偏好）：

  库的 `_normalize_action_function_signature` 在**注册期**用
  `inspect.signature(func)` 取注解，然后跟库自己那份特殊参数类型表做
  `param_type == expected_type` 比较（`registry/service.py:130-155`）。
  而 PEP 563 会把注解**变成字符串**，于是那次比较变成
  `'BrowserSession' == <class BrowserSession>` → False → 注册直接抛：

      ValueError: Action 'extract_table' parameter 'browser_session: BrowserSession'
                  conflicts with special argument injected by tools:
                  'browser_session: BrowserSession'

  ★ 这句话的两边**一模一样**，它是本项目见过的最会误导人的报错：
    名字相同、类型相同，于是第一反应是"库的 bug 吧"，而真相是
    "我们把自己的注解字符串化了，库拿到的是个 str"。

  对照实验（同一次运行，只改这一个 import）：
      with __future__=True  -> ValueError: ... conflicts with special argument ...
      with __future__=False -> 注册成功
  库自己的 `tools/service.py` 也**没有**用这个 import —— 这不是巧合，
  它是"库在运行时读注解"这一设计的必要前提。

  ⚠️ 顺带一提：`guard_gate.py` 里那个 action **没有**特殊参数，
    所以它带着这个 import 也能跑 —— 这正是这个坑危险的地方：
    **它取决于 action 有没有特殊参数，而不是取决于有没有写那行 import。**
    照着"另一个文件也这么写、它能跑"去抄，会在加 `browser_session` 时炸。
"""
import json
from typing import Any

from browser_use import ActionResult, BrowserSession, Tools
from pydantic import BaseModel, ConfigDict, Field

EXTRACT_TABLE_ACTION = "extract_table"
"""动作名。★ 必须是**函数名**：库用 `func.__name__` 当动作名，装饰器上没有
单独传名字的口子（见 tests/test_compat.py 对 Registry.action 签名的断言）。
所以这个常量和 `register_extract_table` 里那个内层函数名是同一件事，改一个必须改另一个。"""


class ExtractTableAction(BaseModel):
    """`extract_table` 的参数。

    ★ `extra="forbid"` 与项目其他模型一致：LLM 多编一个字段就报错触发重试，
      好过一个"多了个来源不明字段"的成功。
    """

    model_config = ConfigDict(extra="forbid")

    table_index: int = Field(
        default=0,
        ge=0,
        description="页面上第几个表格，从 0 开始数。默认第一个。",
    )
    max_rows: int = Field(
        default=100,
        ge=1,
        le=500,
        description="最多读取多少行。超出部分丢弃，并在结果里标 truncated。",
    )


# ★ 走 page.evaluate 而不是 CDP：库的 get_current_page() 给的对象自带 evaluate，
#   一行搞定；用裸 CDP 要自己处理 session / executionContextId 的生命周期，
#   多出来的几十行每一行都要单独验。这里没有性能诉求（一个表格几十行），
#   所以选"少写、少错"的那条。
#
#   ⚠️⚠️ 但那个对象的 evaluate **不是 playwright 的 evaluate**，返回值契约是反的：
#      `get_current_page()` 返回的是 `browser_use.actor.page.Page`，而它的
#      `evaluate(page_function, *args) -> str` **永远返回字符串**
#      （对象 json.dumps、None 变空串、数字布尔变 str()）。
#      这里踩过：写成 `payload: dict = await page.evaluate(...)` 直接
#      `payload.get(...)`，结果是 `'str' object has no attribute 'get'` ——
#      浏览器里 JS 一次都没错、数据全都对，只是被字符串化了一层。
#      解析由 `_decode_table_payload` 显式负责，理由写在那里的 docstring。
#
# ★ 用 t.tHead / t.tBodies 而不是手写 querySelectorAll('thead tr')：
#   浏览器已经把表格结构解析好了，自己再查一遍等于重新实现一遍 HTML 表格模型，
#   而且会更差 —— 直接挂在 <table> 下的 <tr>（不写 tbody）是合法 HTML，
#   浏览器会自动补 tbody，手写选择器却会漏掉那种形态。
#
# ★ 单元格文本 innerText 优先、textContent 兜底：
#   innerText 折叠空白并**跳过不可见元素**（display:none 的内容不该被抄进价格），
#   但它依赖布局，对脱离文档流的元素会返回空串 —— 那时用 textContent 兜。
_TABLE_JS = r"""
(arg) => {
  const tables = Array.from(document.querySelectorAll('table'));
  const t = tables[arg.tableIndex];
  if (!t) {
    return {found: false, tableCount: tables.length};
  }
  const txt = (el) => {
    const s = (el.innerText !== undefined && el.innerText !== null && el.innerText !== '')
      ? el.innerText
      : (el.textContent || '');
    return s.replace(/\s+/g, ' ').trim();
  };

  const headRows = t.tHead ? Array.from(t.tHead.rows) : [];
  let bodyRows = [];
  if (t.tBodies && t.tBodies.length) {
    for (const tb of Array.from(t.tBodies)) bodyRows.push(...Array.from(tb.rows));
  } else {
    const inHead = new Set(headRows);
    bodyRows = Array.from(t.rows).filter((r) => !inHead.has(r));
  }

  const headers = headRows.length
    ? Array.from(headRows[headRows.length - 1].cells).map(txt)
    : [];
  const all = bodyRows.map((tr) => Array.from(tr.cells).map(txt));
  const truncated = all.length > arg.maxRows;
  return {
    found: true,
    tableCount: tables.length,
    headers: headers,
    rows: truncated ? all.slice(0, arg.maxRows) : all,
    totalRows: all.length,
    truncated: truncated,
  };
}
"""


def _decode_table_payload(raw: Any) -> dict[str, Any] | None:
    """把 `page.evaluate` 的返回值解成 dict。读不懂就返回 None。

    ★★ 这里是一条**返回值契约的反转**，本项目吃过一次：

      `browser_session.get_current_page()` 返回的是
      `browser_use.actor.page.Page`（**不是 playwright 的 Page** ——
      它连 `.url` 属性都没有，那是另一个同名类带来的直觉陷阱）。
      而它的签名是：

          async def evaluate(self, page_function: str, *args) -> str

      docstring 原文：*"String representation of the JavaScript execution
      result. Objects and arrays are JSON-stringified."* ——
      **它永远返回字符串**：对象走 `json.dumps`，`None` 变空串 `''`，
      数字/布尔走 `str(...)`。

    ★ 不知道这条的代价（实测原样）：写完
      `payload: dict = await page.evaluate(...)` 之后 `payload.get("found")`，
      得到的是 `'str' object has no attribute 'get'`。这个报错**指向完全错误
      的方向** —— 它读起来像"页面上没有表格"，而真相是浏览器里 JS 执行得
      完美、数据一个字都没错，只是外面多裹了一层引号。

    ★ 所以解析必须是显式的，而且三种坏形态都要有明确处置，而不是抛：
        · 空串          → JS 返回了 null/undefined（本脚本不该发生）；
        · json 解析失败 → 返回了非 JSON 的裸串；
        · 解出来不是 dict → 形状变了（比如哪天改成直接返回数组）。
      三种都返回 None，交给调用方转成一条 LLM 看得懂的 error。
      ⚠️ 修好之后**不要**顺手把这里改成"猜"（比如把裸串当表格文本）：
        静默猜错会让"读不到"变成"读到了一堆垃圾"，而后者看起来像成功。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ★ 公开别名：`extract_cards` 也要解同一个东西（同一个契约反转、同一份实现）。
#   为什么不在那边再抄一遍：两份"看着一模一样"的解码器会各自演化，
#   于是两个采集器对同一个页面的行为出现差异，而差异的原因无从查起
#   —— spike_lib.py 顶部记过同一条教训（转出而不是拷贝）。
decode_eval_payload = _decode_table_payload


async def extract_table_impl(
    params: ExtractTableAction,
    browser_session: BrowserSession,
) -> ActionResult:
    """实现本体。★ 单测直接调它，不必模仿库的 kwargs-only 调用约定。

    ★ 必须 async：`browser_session.get_current_page()` 是协程
      （实测 `inspect.iscoroutinefunction` 为 True）。写成同步的话库会把它
      丢进 `asyncio.to_thread`（归一化包装器的末尾），于是在子线程里
      拿不到运行中的事件循环 —— 报的错会指向别处，指不到"这个 action 该是 async"。
    """
    page = await browser_session.get_current_page()
    if page is None:
        # ★ 返回 error 而不是抛异常：抛出去会被 Tools.act 的
        #   `except Exception` 吞成 ActionResult(error=...)，结果一样但理由丢了。
        #   直接返回 error，让 LLM 看到原因（它可以先 navigate 再重试）。
        return ActionResult(
            error=(
                "extract_table 失败：当前没有可用页面（浏览器还没导航到任何 URL）。"
                "先导航到目标页面再调用。"
            )
        )

    try:
        raw = await page.evaluate(
            _TABLE_JS, {"tableIndex": params.table_index, "maxRows": params.max_rows}
        )
    except Exception as exc:  # noqa: BLE001 —— 原因要原样带给 LLM，不吞
        return ActionResult(
            error=f"extract_table 失败：取表脚本执行出错（{type(exc).__name__}: {exc}）。"
        )

    # ★ 必须显式解析：那个 evaluate 返回的是 JSON 字符串（见 _decode_table_payload）
    payload = _decode_table_payload(raw)
    if payload is None:
        return ActionResult(
            error=(
                f"extract_table 失败：取表脚本的返回值读不懂"
                f"（{str(raw)[:200]!r}）。"
                "正常情况下它应该是一段 JSON 对象；不是的话说明取表脚本"
                "或页面结构与预期不符。"
            )
        )

    if not payload.get("found"):
        return ActionResult(
            error=(
                f"extract_table 失败：页面上没有第 {params.table_index} 个表格"
                f"（共找到 {payload.get('tableCount', 0)} 个）。"
                "确认页面已加载完成、并且确实停在商品列表页。"
            )
        )

    data = {
        "table_index": params.table_index,
        "headers": payload["headers"],
        "rows": payload["rows"],
        "total_rows": payload["totalRows"],
        "truncated": payload["truncated"],
    }
    return ActionResult(
        # ★ ensure_ascii=False：中文商品标题要原样落进 steps.jsonl。
        #   转义成 \uXXXX 也"能读"，但审计日志是给人看的 ——
        #   一份要先解码才能读的日志，等于把排查成本转嫁给看的人。
        extracted_content=json.dumps(data, ensure_ascii=False),
        # ★ long_term_memory 只放摘要，不放整份 JSON：它会进 LLM 后续每一步的上下文。
        #   100 行 × 5 列塞进去，等于后面每一步都在为同一份表格付 token，
        #   还会把真正要看的东西从窗口里挤出去。完整数据在 extracted_content 里
        #   （且已落进 steps.jsonl，那才是审计要看的地方）。
        long_term_memory=(
            f"已确定性读取第 {params.table_index} 个表格：{len(payload['rows'])} 行"
            + (f"（共 {payload['totalRows']} 行，已截断）" if payload["truncated"] else "")
            + "。以上为页面原文，未经模型转录。"
        ),
    )


def register_extract_table(tools: Tools) -> None:
    """把 `extract_table` 注册进一个**已有的** Tools 实例。

    ★ 为什么是"往实例里注册"，而不是返回一个新 Tools：
      `ActionModel` 是 Agent 构造时按**那个实例的**注册表生成的
      （`agent/service.py:786-790`）。先造 Tools → 注册 → 把**同一个实例**
      交给 `Agent(tools=...)`，三者必须指向同一个对象。
      用两个 Tools 实例（一个注册了、一个没注册）是这类接线最经典的错法，
      表现却是 `guard_notice` 那种"模型里没这个字段"的 ValidationError，
      报错位置离真正的错处很远。

    ★ 为什么接收 `tools` 而不是自己造：注册顺序要**可枚举**。
      所有自定义 action 的注册都发生在同一个地方（`actions/__init__.py` 的
      `build_tools`），这样"到底注册了哪些动作"是一个能被读出来的列表，
      而不是散在各处的副作用。
    """

    @tools.action(
        "确定性地读取页面上的一个表格，返回 JSON（headers + rows，单元格为页面原文）。"
        "自己不经手任何模型转录 —— 价格/库存这类字段应该用它，而不是凭页面文本记忆填写。",
        param_model=ExtractTableAction,
        # ★ terminates_sequence 保持默认 False：它是"执行后丢弃本批剩余动作"的开关，
        #   读完一个表格没有理由丢掉同批次的其他动作。
    )
    async def extract_table(  # noqa: F811 —— 与模块级常量同名是有意的，见下
        params: ExtractTableAction,
        browser_session: BrowserSession,
    ) -> ActionResult:
        # ★★ 内层这个函数名不能为了"避免遮蔽"改掉：库用 `func.__name__`
        #   当动作名，所以**这个名字就是动作名本身**。实现放在
        #   `extract_table_impl` 里，名字留在这里，两个约束就都满足了 ——
        #   而单测测的是 impl，不必模仿库的 kwargs-only 调用约定
        #   （"模仿库的调用约定"正是这个项目反复吃亏的地方）。
        return await extract_table_impl(params, browser_session)


async def verify_extract_table_round_trip(tools: Tools) -> None:
    """启动期门禁：**真的按库的方式调一次** `extract_table`，看接线通不通。

    ★★ 为什么是"真的调一次"，而不是"查 ActionModel 里有没有这个字段"：

      查字段名只能证明"它被注册了"，证明不了**库会怎么调它** —— 而这正是
      Phase 3 那次 live 失败的全部教训（`should_stop` 签名看着没问题，
      坏在调用点：见 interceptor.py 的 `verify_stop_callback`）。
      `extract_table` 的接线比那个还多一层，它同时依赖三件事：
        · `param_model=` 显式给了（否则 `params` 会被当成给 LLM 的字段）；
        · 第一个参数名叫 `params` 且**不是**特殊参数名（否则它被跳过/被注入）；
        · `browser_session` 注解类型正确（不符时库抛
          "conflicts with special argument injected by tools"，读起来像重名问题）。
      这三条任何一条错了，报错都指不到原因，而症状都是"LLM 调了但没反应"。

    ★ 怎么做到"不需要浏览器"就能端到端验：
      给一个 `get_current_page()` 返回 None 的**桩会话**。接线正确时，
      调用会一路走到 `extract_table_impl`，然后返回"当前没有可用页面"那条 error。
      于是四件事一次全证：
        1. 动作在注册表里、能按名字取到；
        2. `params=` 这个 kwargs-only 契约被满足（库在 wrapper 里对位置参数直接抛 TypeError）；
        3. `browser_session` 被库正确地**注入**进了实现（否则实现会 AttributeError）；
        4. 实现体真的被执行到，返回的是 ActionResult。
      任何一条不成立，这里当场抛 —— 启动失败远好过"动作看起来注册了但调不动"。

    ★★ 为什么这个函数必须是 `async def`（而不是 `def` 里 `asyncio.run(...)`）：

      它唯一的调用点在 `runtime/runner.py` 的 `TaskRunner.run()` 里，
      而那是一个**协程**。在协程里调 `asyncio.run()` 会当场抛
      `RuntimeError: asyncio.run() cannot be called from a running event loop` ——
      也就是说，这个自检**在它唯一存在的场景里一次都跑不成**。

      这条是 e2e 跑出来的（`tests/test_mock_pdd_e2e.py` 的护栏用例）：
      所有离线单测都是**同步**调用它，于是它们全绿；而生产路径是异步的，
      于是一跑真 run 就崩在第一行。**"自检本身没被任何东西挡住"这件事，
      恰恰要靠"真的走一遍生产路径"才能验出来** —— 单测再全也覆盖不到
      "调用者的颜色（sync/async）对不对"。

      ⚠️ 顺带一个更隐蔽的形态：`asyncio.run()` 抛错时，那个它本该 await 的
        协程**从未被 await**，于是 Python 只打一条
        `RuntimeWarning: coroutine '...' was never awaited`。那种警告默认
        不致命、容易淹在几十条 warning 里 —— 而它是"这个函数根本没执行"
        的唯一线索。
    """

    class _NullSession:
        """只回答 `get_current_page() → None` 的桩。

        ★ 刻意**不**继承 BrowserSession：继承会带来一整套构造期依赖
          （CDP、profile、playwright），而这里要验的是**注册表接线**，
          不是浏览器能不能起。用继承的话，这个门禁就变成"起得来浏览器才跑"，
          那它就不再是启动自检了 —— 而启动自检的全部价值就在于它比被测对象先跑。
        """

        async def get_current_page(self) -> None:
            return None

    registry = getattr(getattr(tools, "registry", None), "registry", None)
    actions = getattr(registry, "actions", None) or {}
    entry = actions.get(EXTRACT_TABLE_ACTION)
    if entry is None:
        raise RuntimeError(
            f"注册表里没有 {EXTRACT_TABLE_ACTION} —— 动作名是**函数名**（库用 func.__name__），"
            f"所以 register_extract_table 里那个内层函数被改名了。"
            f"当前注册的动作：{sorted(actions)}"
        )

    try:
        result = await entry.function(params=ExtractTableAction(), browser_session=_NullSession())
    except Exception as exc:  # noqa: BLE001 —— 原因要原样带出去，不吞
        raise RuntimeError(
            f"{EXTRACT_TABLE_ACTION} 的接线不通：{type(exc).__name__}: {exc}。"
            f"最常见的是 param_model= 没给、或者 params 参数的注解/名字不对"
            f"（契约说明见本文件顶部）。"
        ) from exc

    if not isinstance(result, ActionResult) or not result.error:
        raise RuntimeError(
            f"{EXTRACT_TABLE_ACTION} 用桩会话调用后，期望拿到一条"
            f"'没有可用页面'的 ActionResult(error=...)，实际拿到 {result!r} —— "
            f"说明实现体没有走到预期的分支。"
        )
