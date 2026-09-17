"""`extract_cards` —— 确定性地把**卡片列表**页面读成结构化行。

★★ 为什么要有它：真站点上撞见的第一种"形态差异"

  `extract_table` 只认 `<table>`。而拼多多商家后台的「机会商品」页
  （`/goods/goods_list/chance`，S8 探路实测）是**卡片网格**：35 张商品卡，
  每张是一个 `<span>`（内含 1 张图 + 4 行文本），整页 `<table>` 数为 **0**。
  于是同一套 DSL / 护栏 / 落库，在商品列表页能采到行，在这一页**一行都采不到**。

  ⚠️ 而"采不到"和"页面是空的"在产物里长得**一模一样** —— 又是那个反复出现的家族：
     前端读一个字段，而某个通道从没发过它。

★★ 判据是**结构**，不是 class 名：

      「同一父节点下 ≥N 个『同标签 + 同 class』的兄弟 = 一个列表」

  ★ 为什么不用 class 名：真实站点的 class 是构建产物 hash，随发版变。照抄它等于
    给自己埋一个**必然过期**的断言（与"mock 站点不复制真实站点的 class 名"同一条理由）。
    "同父同形兄弟"这条对 class 名**完全不敏感** —— 它认的是"重复"这件事本身，
    而"列表"在 DOM 里的定义就是重复。
  ★ 这条判据在 S8 探路里一次就命中了那 35 张卡，**顺带**也把 30 个行内埋点
    `<script>` 当成了一组 —— 所以必须滤掉不渲染的标签（见 `_CARDS_JS` 的 SKIP）。
    那次"探测器忠实地回答了一个问错了对象的问题"，是这条过滤存在的全部理由。

★★ 值的转录仍然**不经过 LLM**，而且判据写在 YAML 里（见 `dsl/models.py` 的 CardField）：

  每张卡要取哪些字段、怎么从文本里认出它们，由任务定义的 `card_fields` 给出，
  逐行匹配。采集器只执行。判据走**闭包**进 action（本项目往自定义 action 传对象的
  唯一姿势，见 README 的 ADR / 引擎事实 3），所以模型能决定的只有
  "读第几组、读多少"——**不能**决定"哪一行算价格"。

  ⚠️ **这句话的边界**（不写清楚会被读成比实际更强的保证，而那是本项目最忌讳的事）：
    它说的是**这一步**。本动作返回的 JSON 里每个值都是页面原文、逐字未加工；
    但一次 run 的最终产物仍然由模型写出来 —— `done` → 结构化输出 → sqlite 的
    products 行，模型要从这份 JSON 抄进 `output_model`。所以端到端看，值仍然
    过了一次 LLM 的手；区别在于那一手拿到的是**已经分好字段、带 missing 计数的
    文本**，而不是一整页原文（后者正是库内置 `extract` 的输入）。
    这与 `extract_table` 完全同形，不是本动作引入的新问题。
    抄错能被**看见**（pydantic 校验 + `_product_payload` 的 sanity 标记），
    但**不能**被阻止 —— 这两件事不要混着说。

★★ 三种失败形态，各自要指向不同的处置：

  · 页面上没有够大的重复块   → error，**并附上找到的组清单**（有哪几组、各多少个元素）
  · group_index 指到不存在的组 → error，同上 —— 这是"抓错组"唯一可诊断的形态
  · 某个字段在所有卡上都没匹配上 → **不报错**：该字段全为空 + missing 计数进结果

  ★ 最后一条为什么不报错：那是"判据写错了"，而判据在 YAML 里，人改一行就好。
    报错会让 LLM 去"想办法"，而它的办法通常是改用库内置的 `extract`
    —— 价格于是被转录一遍，错误就从**看得见的空**变成**看不见的错**。
    空值 + missing 计数是"喊人来看"，不是"让模型自己解决"。

★ 与 `extract_table` 的关系：同一套形状（零 LLM、读 DOM 原文、失败可见、
  不配 `domains=`、必须有 `verify_*_round_trip` 启动自检），只是取数判据不同。
  两个动作**不是**二选一 —— LLM 按页面形态选：有 `<table>` 用前者，卡片网格用后者。
"""
import json
import re
from collections import Counter
from typing import Any, Sequence

from browser_use import ActionResult, BrowserSession, Tools
from pydantic import BaseModel, ConfigDict, Field

from ecom_agent.actions.extract_table import decode_eval_payload
from ecom_agent.dsl.models import CardField

CARDS_ACTION = "extract_cards"
"""动作名。★ 必须是**函数名**（库用 `func.__name__` 当动作名，装饰器上没有单独传
名字的口子）。所以它和 `register_extract_cards` 里那个内层函数名是同一件事，
改一个必须改另一个 —— `test_extract_cards.py` 里有一条专门钉这个。"""


class ExtractCardsAction(BaseModel):
    """`extract_cards` 的参数。

    ★★ `card_fields`（字段判据）**故意不在这里**：这里的字段是给 LLM 填的，
      而判据必须由人 review。判据走闭包进 action，见本文件顶部。

    ★ `extra="forbid"` 与项目其他参数模型一致：模型多编一个字段就报错触发重试，
      好过一个"多了个来源不明字段"的成功。
    """

    model_config = ConfigDict(extra="forbid")

    group_index: int = Field(
        default=0,
        ge=0,
        le=20,
        description=(
            "页面上第几组重复块，从 0 开始，**按成员个数从多到少排**。"
            "默认 0 = 成员最多的那一组，通常就是主列表。"
            "若结果里的行不是你要的东西，看错误信息里的组清单换一个序号。"
        ),
    )
    min_members: int = Field(
        default=3,
        ge=2,
        le=200,
        description="一组至少要有几个同形兄弟才算列表。低于这个数的不参与编号。",
    )
    max_cards: int = Field(
        default=50,
        ge=1,
        le=200,
        description="最多读多少张卡，超出的丢弃并在结果里标 truncated。",
    )


# ★ 一段卡片的文本上限：超过它的多半是布局容器而不是列表项（S8 实测：
#   一个 3 个成员的"跨境/社区团购"推广位整块文本有 200 多字，那不是列表）。
_MAX_MEMBER_CHARS = 300

# ★★ "没有可用页面"那条 error 的判据短语，**实现与启动门禁共用这一个常量**。
#    门禁靠"error 里有没有它"判断实现走到了哪条分支（见 verify_extract_cards_round_trip），
#    所以两边一漂移门禁就会误判 —— 而这正是本项目反复吃亏的那件事：
#    两份"看着一模一样"的字符串各自演化。
_NO_PAGE_MARKER = "当前没有可用页面"

# ★ 不渲染的标签要整个跳过。理由见本文件顶部那段"探测器的对象问错了"。
_CARDS_JS = r"""
(arg) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const SKIP = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'NOSCRIPT', 'TEMPLATE', 'HEAD']);

  const groups = [];
  for (const p of Array.from(document.querySelectorAll('*'))) {
    if (SKIP.has(p.tagName)) continue;
    const buckets = new Map();
    for (const c of Array.from(p.children)) {
      if (SKIP.has(c.tagName)) continue;
      const cls = typeof c.className === 'string' ? c.className : '';
      const key = c.tagName + '|' + cls;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(c);
    }
    for (const [key, kids] of buckets) {
      if (kids.length < arg.minMembers) continue;
      const sample = norm(kids[0].innerText);
      if (!sample || sample.length > arg.maxMemberChars) continue;
      groups.push({tag: key.split('|')[0], members: kids, sample: sample.slice(0, 100)});
    }
  }

  // ★ 按成员个数降序。JS 的 sort 是稳定的，所以数量相同的组保持 DOM 顺序 ——
  //   于是 group_index 在同一个页面上是**可复现**的（换个浏览器也一样）。
  groups.sort((a, b) => b.members.length - a.members.length);

  const inventory = groups.slice(0, 5).map((g) => ({
    tag: g.tag, memberCount: g.members.length, sample: g.sample,
  }));

  const picked = groups[arg.groupIndex];
  if (!picked) {
    return {found: false, groupCount: groups.length, inventory: inventory};
  }

  const all = picked.members.map((el) =>
    (el.innerText || '')
      .split('\n')
      .map((s) => s.replace(/\s+/g, ' ').trim())
      .filter((s) => s)
  );
  const truncated = all.length > arg.maxCards;
  return {
    found: true,
    groupCount: groups.length,
    inventory: inventory,
    pickedTag: picked.tag,
    pickedMembers: picked.members.length,
    cards: truncated ? all.slice(0, arg.maxCards) : all,
    totalCards: all.length,
    truncated: truncated,
  };
}
"""


def _apply_fields(
    lines: Sequence[str], fields: Sequence[tuple[str, re.Pattern[str]]]
) -> tuple[dict[str, str], set[str]]:
    """把字段判据套到**一张卡**的逐行文本上。返回 (行字典, 没匹配上的字段名集合)。

    ★ 逐行匹配、**同一字段取第一个命中的行**（不是第一次出现的位置）：
      S8 实测的卡片是四行 —— `['<商品标题>', '¥15.1', '热度 82', '发布同款']`，
      一个字段一行。按行匹配让"第一个命中"这件事有确定含义，而不是在一整块文本里
      猜边界。
      ⚠️ 标题那一格是**占位符**：真实商品标题不进仓库（README 已声明演示数据全部
      来自本地 mock 站）。四行的**形态**与后三行是实测值 —— 留着它们是因为正则需要
      真实的书写形式（`¥` 和数字之间到底有没有空格，直接决定判据该怎么写）。

    ★ 有捕获组取第 1 组、没有就取整个匹配：这条规矩写进了 CardField 的字段说明里，
      因为它是使用者在写 YAML 时要依赖的东西 —— 不写在文档里的隐含规矩等于没有。
    """
    row: dict[str, str] = {}
    missing: set[str] = set()
    for name, regex in fields:
        value = ""
        for line in lines:
            m = regex.search(line)
            if m:
                value = (m.group(1) if m.groups() else m.group(0)).strip()
                break
        if not value:
            missing.add(name)
        row[name] = value
    return row, missing


def _describe_inventory(payload: dict[str, Any]) -> str:
    """把"找到了哪几组"说成一句能照着行动的话。

    ★ 这一段是"抓错组"唯一的出路：不含它，error 只能写"没找到列表"，
      而人在真站点上没法据此判断该把 group_index 改成几。
    """
    inventory = payload.get("inventory") or []
    if not inventory:
        return "页面上没有找到任何重复块（连一组够大的都没有）"
    parts = [
        f"[{i}] {g.get('memberCount')} 个 <{g.get('tag')}> 例：{(g.get('sample') or '')[:40]!r}"
        for i, g in enumerate(inventory)
    ]
    return "找到的重复块（按成员数降序，最多列 5 组）：" + "；".join(parts)


async def extract_cards_impl(
    params: ExtractCardsAction,
    browser_session: BrowserSession,
    *,
    fields: Sequence[CardField],
) -> ActionResult:
    """实现本体。★ 单测直接调它，不必模仿库的 kwargs-only 调用约定。

    ★ 必须 async：`get_current_page()` 是协程（见 extract_table.py 的同名说明）。
    """
    if not fields:
        # ★ 这条不是"配置忘了"的兜底，而是**唯一能说清为什么不报错**的地方：
        #   没有判据就没法取字段，而"取不了"必须说给 LLM 听，让它要么换个动作、
        #   要么停下汇报 —— 而不是让它自己发明一套字段名。
        return ActionResult(
            error=(
                "extract_cards 失败：本任务的 YAML 里没有配 card_fields（字段判据），"
                "所以它不知道每张卡要取哪些字段。"
                "请改用 extract_table，或让模板作者补上 card_fields 再来。"
            )
        )

    page = await browser_session.get_current_page()
    if page is None:
        return ActionResult(
            error=(
                f"extract_cards 失败：{_NO_PAGE_MARKER}（浏览器还没导航到任何 URL）。"
                "先导航到目标页面再调用。"
            )
        )

    arg = {
        "groupIndex": params.group_index,
        "minMembers": params.min_members,
        "maxCards": params.max_cards,
        "maxMemberChars": _MAX_MEMBER_CHARS,
    }
    try:
        raw = await page.evaluate(_CARDS_JS, arg)
    except Exception as exc:  # noqa: BLE001 —— 原因要原样带给 LLM，不吞
        return ActionResult(
            error=f"extract_cards 失败：取卡片脚本执行出错（{type(exc).__name__}: {exc}）。"
        )

    # ★ 必须显式解析：那个 evaluate 返回的是 JSON **字符串**（契约反转，
    #   完整经过见 extract_table.py 的 decode_eval_payload）。
    payload = decode_eval_payload(raw)
    if payload is None:
        return ActionResult(
            error=(
                f"extract_cards 失败：取卡片脚本的返回值读不懂（{str(raw)[:200]!r}）。"
                "正常情况下它应该是一段 JSON 对象。"
            )
        )

    if not payload.get("found"):
        return ActionResult(
            error=(
                f"extract_cards 失败：页面上没有第 {params.group_index} 组重复块"
                f"（共找到 {payload.get('groupCount', 0)} 组）。{_describe_inventory(payload)}。"
                "若上面列出的某一组才是你要的列表，用它前面的序号当 group_index 重试。"
            )
        )

    compiled: list[tuple[str, re.Pattern[str]]] = [
        (f.name, re.compile(f.pattern)) for f in fields
    ]
    cards: list[list[str]] = payload["cards"]
    rows: list[dict[str, str]] = []
    missing = Counter()
    for lines in cards:
        row, miss = _apply_fields(lines, compiled)
        rows.append(row)
        for name in miss:
            missing[name] += 1

    # ★ missing 全量输出（包括计数为 0 的字段）—— "这一列全都取到了"和
    #   "这一列压根没出现在报告里"是两件事，而后者会被读成前者。
    missing_all = {name: missing.get(name, 0) for name, _ in compiled}

    data = {
        "group_index": params.group_index,
        "group_tag": payload.get("pickedTag"),
        "group_members": payload.get("pickedMembers"),
        "fields": [name for name, _ in compiled],
        "rows": rows,
        "returned": len(rows),
        "total_cards": payload.get("totalCards", len(rows)),
        "truncated": bool(payload.get("truncated")),
        "missing": missing_all,
    }
    return ActionResult(
        # ★ ensure_ascii=False：中文商品标题要原样落进 steps.jsonl（审计日志是给人看的）。
        extracted_content=json.dumps(data, ensure_ascii=False),
        # ★ long_term_memory 只放摘要，不放整份 JSON：它会进 LLM 后续每一步的上下文。
        long_term_memory=(
            f"已确定性读取第 {params.group_index} 组重复块（{data['group_tag']}，"
            f"共 {data['group_members']} 个元素）：{len(rows)} 张卡"
            + (f"（原文共 {data['total_cards']} 张，已截断）" if data["truncated"] else "")
            + f"。字段：{'、'.join(data['fields'])}"
            + _missing_note(missing_all)
            + "。以上为页面原文，未经模型转录。"
        ),
    )


def _missing_note(missing: dict[str, int]) -> str:
    """把 missing 说成一句人话；全取到时给一句明确的"都取到了"。

    ★ 为什么要给"都取到了"这句：一句话的缺席和一句话说"没问题"在报告里长得不一样，
      而人只会去看有字的地方。missing 计数全是 0 时留一句，等于明确宣告"这一项我查过"。
    """
    bad = {k: v for k, v in missing.items() if v}
    if not bad:
        return "（每个字段都匹配上了）"
    return "；⚠️ 这些字段有卡片没匹配上：" + "、".join(f"{k} 缺 {v} 张" for k, v in bad.items())


def register_extract_cards(tools: Tools, *, fields: Sequence[CardField] = ()) -> None:
    """把 `extract_cards` 注册进一个**已有的** Tools 实例。

    ★ `fields` 是**闭包**进来的，不是 action 参数（引擎事实 3：给自定义 action
      传对象只能用闭包；`Agent(context=...)` 那条路连接收都没接收）。
      于是字段判据对 LLM 是**只读**的 —— 与护栏条款不由 LLM 说了算同源。
    """
    field_names = "、".join(f.name for f in fields) or "（本任务未声明 card_fields）"

    @tools.action(
        "确定性地读取页面上的一个**卡片列表**（同一容器里重复出现的同形元素，"
        "例如商品卡网格），按任务预先声明的字段判据逐张取值，返回 JSON。"
        "页面是卡片/网格布局、没有 <table> 时用它；有 <table> 时用 extract_table。"
        f"本任务声明的字段：{field_names}。",
        param_model=ExtractCardsAction,
    )
    async def extract_cards(  # noqa: F811 —— 与模块级常量同名是有意的（函数名即动作名）
        params: ExtractCardsAction,
        browser_session: BrowserSession,
    ) -> ActionResult:
        return await extract_cards_impl(params, browser_session, fields=fields)


async def verify_extract_cards_round_trip(tools: Tools, *, fields: Sequence[CardField] = ()) -> None:
    """启动期门禁：**真的按库的方式调一次** `extract_cards`，看接线通不通。

    ★ 与 `verify_extract_table_round_trip` 同源，理由也同源（见那里的长说明）：
      这个动作同样带特殊参数 `browser_session`，所以"param_model= 给没给、
      参数名对不对、注解类型对不对"三条错法都会在注册期炸，而报错都指不到原因。
      用桩会话真调一次，接线不对就在这里死，而不是等 run 跑起来才表现为
      "LLM 调了但没反应"。

    ★★ 与 extract_table 那一条的关键差异：这条还顺手验了**闭包**，而验法是
      "看返回值说了什么"，不是"往调用里塞参数"。

      为什么不能把 `fields` 塞进这次调用：它不是被调函数的参数 ——
      它是**注册时闭进去**的（`register_extract_cards(tools, fields=...)`），
      那个内层函数的签名里根本没有它。而 `entry.function` 是库归一化之后的
      wrapper，它认哪些 kwargs 由库的签名契约决定，不由我们决定 ——
      往里塞一个库里没有的名字，是在拿一个**未定义契约**试运气：
      运气好被忽略、运气坏 TypeError，而两种结果都不说明接线对不对。

      于是改成看 error 的措辞。闭包里真带着判据时，实现会走到"没有可用页面"
      那条分支；闭包是空的时候，它会**先**撞上"没配 card_fields"那条。
      两条 error 措辞不同，所以"闭包没接上"是一个**可判定的**形态 ——
      而这正是这条门禁存在的意义：不这么写的话，它在任何情况下都通过，
      包括"runner 忘了把 card_fields 传进 build_tools"那种真错误。

    ★ 本任务不用 extract_cards 时（`fields` 为空）直接跳过、不报错：
      一个任务不用某个动作是合法的。注意这与上面那条**不矛盾** ——
      传进来的 `fields` 是"我们的预期"，闭包实际带了什么是被验的对象。
    """
    if not fields:
        return

    class _NullSession:
        """只回答 `get_current_page() → None` 的桩（不继承 BrowserSession，理由同 extract_table）。"""

        async def get_current_page(self) -> None:
            return None

    registry = getattr(getattr(tools, "registry", None), "registry", None)
    actions = getattr(registry, "actions", None) or {}
    entry = actions.get(CARDS_ACTION)
    if entry is None:
        raise RuntimeError(
            f"注册表里没有 {CARDS_ACTION} —— 动作名是**函数名**（库用 func.__name__），"
            f"所以 register_extract_cards 里那个内层函数被改名了。"
            f"当前注册的动作：{sorted(actions)}"
        )

    try:
        result = await entry.function(params=ExtractCardsAction(), browser_session=_NullSession())
    except Exception as exc:  # noqa: BLE001 —— 原因要原样带出去，不吞
        raise RuntimeError(
            f"{CARDS_ACTION} 的接线不通：{type(exc).__name__}: {exc}。"
            f"最常见的是 param_model= 没给、或者 params 参数的注解/名字不对"
            f"（契约见 extract_table.py 顶部那段）。"
        ) from exc

    if not isinstance(result, ActionResult) or not result.error:
        raise RuntimeError(
            f"{CARDS_ACTION} 用桩会话调用后，期望拿到一条错误 ActionResult，实际拿到 {result!r}。"
        )

    if _NO_PAGE_MARKER not in result.error:
        raise RuntimeError(
            f"{CARDS_ACTION} 的门禁没走到预期分支。期望一条含 {_NO_PAGE_MARKER!r} 的错误"
            f"（桩会话没有页面），实际是：{result.error!r}。"
            f"★ 如果这条 error 说的是「没配 card_fields」，那说明"
            f"**注册时的闭包是空的** —— 也就是 build_tools(card_fields=...) 那一环没接上，"
            f"而本任务的 YAML 里明明声明了 {len(fields)} 个字段判据。"
        )
