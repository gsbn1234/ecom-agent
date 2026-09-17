"""`sanity_flags()` / `_detect_pii()` / `suspicious` —— 以及它们底下的 `Redactor`。

★ 为什么会有这个文件：这三处是「可疑数据只标记不删除」（ADR 11）的实现，
  此前**代码里有、测试里没有**（README 缺口节第 2 条）。
  写的过程中又核出两件 README 没写的事，一并钉在下面：
    · `Redactor` **整个类**在 `tests/` 下零命中（两条独立检查：没有任何用例断言过
      `<REDACTED>` 产物、没有任何用例 import 过它）—— 缺的不只是那三个名字；
    · `Authorization: Bearer <token>` 这个**最常见的凭证形态**不在脱敏规则的射程内。
      ✅ **2026-09-17 已修**（`redact.py` 加了一条锚在头名上的规则）。当时先钉的是
      `strict xfail`，修完它变成 XPASS 主动失败提醒删标记 —— 这个流程本身跑通了。
      ✅ **2026-09-18 补齐**：`Authorization: Basic <base64>` 与 DRF 的
      `Authorization: Token <key>` 这两个同族实例也盖住了，守卫就在下面
      （含**字符类**那条，和一条**以「已知代价」命名**的用例）。
      补之前本文件里确实没有它们的守卫，README 缺口第 2 条那两行
      「仍一个字都不盖」曾是**唯一一处「文档说了、机制没跟上」** —— 现在收口了。
      ⚠️ 射程**没变**：仍**只覆盖头部形态**，不在头部、单说一句 `Bearer <值>` 的地方不盖。

★ 这里测不到什么，先说清楚（与 `test_extract_cards.py` 同一条纪律）：
  这些 flag **报得对不对**，单测判不了 —— 它只判「哪个字符串在什么条件下出现、
  以什么顺序出现」。"库存 10 万以上多半是把销量填进了库存"这条业务猜测准不准，
  只能靠真数据回看。这里钉的是**机制**：阈值、边界、聚合形态、落到 sqlite 的那一跳。

★ 对照实验（拆掉机制 → 对应那条必须红）。下面六条**都真跑过**（2026-09-17），
  每条都先确认"改了机制这条就红"，改完再**逐字节还原**（md5 复核一致）：
  · `stock > 100_000` 改成 `>=` → `test_the_implausible_stock_boundary_is_strict` 红；
  · `_detect_pii` 改成返回 `probe.counts.values()` → 那条"种类不是次数"红；
  · `save_run` 里的聚合键改成不兜底空 id → `test_the_row_level_aggregate_is_keyed_by_goods_id` 红；
  · `_literals` 的 `reverse=True` 去掉 → 长值优先那条红（会剩半个"铺"字）；
  · `"suspicious": int(bool(flags))` 改成 `0` → `test_suspicious_is_one_when_a_sanity_flag_fires` 红；
  · `_product_payload` 的 `"title": row.title` 改成过一遍 `Redactor` → 保真那条红。

★ 补 `Basic` / `Token` 时（2026-09-18）又跑了**四条**，也都先断言了锚点命中次数：
  · scheme 列表里删掉 `basic` → "三种 scheme 都盖住"那条红；
  · 字符类收回 bearer 的字母表（去掉 `+ / =`）→ base64 那条红；
  · 去掉 `={0,2}` 填充（`==` 留在盘上 = 半截凭证）→ "三种 scheme 都盖住"那条红；
  · 锚点拆成不带头名的笼统形态 → "散文不许被盖"那条红。
  两批加起来 **10 条**，每条都还原后复核过 md5。

★ 写这个文件时我被自己绊了一次，留在这里当反面教材：第一版断言
  `redaction_counts_json == {"literal": 2}`，因为"洗了 2 处"是肉眼可见的事实 ——
  结果红了。因为那一列**不是** `save_run(redactor=...)` 那个参数填的，而是
  `record.redaction_counts` 填的（recorder 盖的章）。真实路径上两者同源所以没问题，
  但"同一个概念在两个地方各有一份来源"这件事本身该被钉住 —— 见
  `test_the_redaction_counts_column_comes_from_the_record_not_from_the_redactor`。
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from ecom_agent.observability.models import RunRecord
from ecom_agent.observability.redact import MIN_LITERAL_LEN, Redactor
from ecom_agent.sites.pinduoduo.output_models import ParseStatus, ProductRow, ProductRowList
from ecom_agent.store.repository import Repository, _detect_pii


# ── 桩 ────────────────────────────────────────────────────
def _row(**kw) -> ProductRow:
    """一行**干净**商品，再用 kw 覆盖出要测的那个毛病。

    ★ 默认值必须干净：这样"某个 flag 出现了"只可能来自被覆盖的那个字段，
      不会来自测试自己顺手塞进去的东西。缺了这个干净的对照，
      "flag 出现了"和"这个字段本来就是脏的"分不开。
    """
    base = dict(
        goods_id="123456",
        title="夏季新款纯棉短袖T恤",
        price=Decimal("9.90"),
        stock=10,
        status="在售中",
    )
    base.update(kw)
    return ProductRow(**base)


def _record(run_id: str = "2026-01-01T00:00:00+00:00-abc123", **kw) -> RunRecord:
    base = dict(task_id="demo.x", status="completed", parse_status=ParseStatus.OK.value)
    base.update(kw)
    return RunRecord(run_id=run_id, **base)


def _save_and_read(tmp_path, rows, *, record=None, redactor=None):
    """存进临时库再原样读回来 —— 走真 schema + 真 executemany，不是构造出来的 dict。

    ★ 为什么端到端而不是只测 `_product_payload`：`suspicious` 这个值要跨三跳
      （`_product_payload` 算 → executemany 绑定 → products 表列），
      只测第一跳的话，"列名绑错了"这种错照样全绿。
    """
    db = tmp_path / "t.db"
    rec = record or _record()
    with Repository(db) as repo:
        repo.save_run(rec, rows, redactor=redactor)
        dups = repo.duplicate_goods_ids(rec.run_id)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    products = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM products WHERE run_id = ? ORDER BY seq", (rec.run_id,)
        )
    ]
    run_row = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (rec.run_id,)).fetchone())
    conn.close()
    return products, run_row, dups


# ── sanity_flags()：阈值与边界 ─────────────────────────────
def test_a_clean_row_has_no_flags():
    """对照组：干净行必须是空列表。上面所有"flag 出现"的断言都以它为前提。"""
    assert _row().sanity_flags() == []


@pytest.mark.parametrize("price", ["0", "-1", "-0.01"])
def test_price_at_or_below_zero_is_flagged(price):
    assert _row(price=Decimal(price)).sanity_flags() == ["price<=0"]


def test_negative_stock_is_flagged_but_zero_stock_is_not():
    """★ 0 库存是**合法状态**（卖光了），负库存才是 LLM 出错。

    这两者被分开是刻意的：合并成 `<= 0` 会让每一行"已售罄"都变成可疑数据，
    而"满屏都是可疑标记"和"没有可疑标记"一样没用 —— 信号得稀缺才是信号。
    """
    assert _row(stock=-1).sanity_flags() == ["stock<0"]
    assert _row(stock=0).sanity_flags() == []


def test_the_implausible_stock_boundary_is_strict():
    """10 万件是**排他**上界：100000 放过，100001 才算可疑。

    ★ 钉的是那个比较符，不是那个数。改成 `>=` 这条立刻红 —— 而"差一位"的
      阈值错误在真实数据上几乎看不出来（两边的库存都"很大"）。
    """
    assert _row(stock=100_000).sanity_flags() == []
    assert _row(stock=100_001).sanity_flags() == ["stock_implausibly_large"]


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_blank_title_is_flagged(title):
    """空串和纯空白算同一件事 —— `not self.title.strip()` 而不是 `not self.title`。"""
    assert _row(title=title).sanity_flags() == ["title_empty"]


@pytest.mark.parametrize("goods_id", ["", "  ", "ABC123", "1234567890123.0", "-1"])
def test_non_numeric_goods_id_is_flagged(goods_id):
    """★ 空 id 也归这一条（`"".isdigit()` 是 False）：空和"不是数字"的处置相同 ——
    两种情况下这个 id 都不能拿去和后台对账，而区分它们没有对应的动作。"""
    assert _row(goods_id=goods_id).sanity_flags() == ["goods_id_not_numeric"]


def test_all_four_flags_can_fire_at_once_and_the_order_is_stable():
    """★★ 五个 flag **永远不可能同时出现**，最多四个。

    `stock < 0` 和 `stock > 100_000` 是互斥的 —— 同一行的库存不可能既负又十万以上。
    写下来是因为"五个检查点"读起来像"五个独立风险"，而实际这个列表的**上界是 4**。
    """
    flags = _row(price=Decimal("0"), stock=-1, title="", goods_id="").sanity_flags()
    assert flags == ["price<=0", "stock<0", "title_empty", "goods_id_not_numeric"]
    assert len(flags) <= 4


# ── all_sanity_flags()：聚合形态 ──────────────────────────
def test_all_sanity_flags_is_empty_for_a_clean_batch():
    """★ 空字典而不是"每个 goods_id 都映射到空列表"—— 否则调用方要自己过滤空值，
    而"过滤"这件事只要有一处忘了，报告上就会多出一堆没问题的行。"""
    assert ProductRowList(rows=[_row(), _row(goods_id="999")]).all_sanity_flags() == {}


def test_all_sanity_flags_only_contains_the_bad_rows():
    got = ProductRowList(rows=[_row(), _row(goods_id="999", price=Decimal("0"))]).all_sanity_flags()
    assert got == {"999": ["price<=0"]}


def test_a_row_without_a_goods_id_gets_a_synthetic_key():
    """空 id 的行用 `<row-N>` 当键 —— 否则它会以空串为键，和别的空 id 行撞在一起。"""
    got = ProductRowList(rows=[_row(goods_id="", stock=-1)]).all_sanity_flags()
    assert got == {"<row-0>": ["stock<0", "goods_id_not_numeric"]}


def test_two_rows_with_the_same_goods_id_collapse_into_one_entry():
    """★★ 已知缺陷（**钉住，不是祝福**）：同 id 的两行，后一行的标记会盖掉前一行。

    实测：`[("777", price<=0), ("777", stock<0)]` → `{"777": ["stock<0"]}`，
    那条 `price<=0` **没了**。原因是 `out[r.goods_id or ...] = f` 是个赋值。

    为什么只是钉住而不算致命：重复 id 本身在 `products` 表里是**能查出来的**
    （`(run_id, seq)` 主键的取舍回报 —— 见 `Repository.duplicate_goods_ids`），
    所以"这次 run 有重复"不会被静默吞掉，被吞掉的只是"重复的那几行各自的标记"。
    要修的话得把它改成 `setdefault(...).extend(...)`，但那会改变
    `all_sanity_flags()` 的返回类型语义（一个 id 对应两行的标记）——
    是一个需要单独拍板的改动，不顺手做。
    """
    got = ProductRowList(
        rows=[_row(goods_id="777", price=Decimal("0")), _row(goods_id="777", stock=-1)]
    ).all_sanity_flags()
    assert got == {"777": ["stock<0"]}  # ← 后来者覆盖，第一行的 price<=0 丢了


# ── _detect_pii()：种类，不是次数 ─────────────────────────
def test_a_clean_title_yields_no_pii_kinds():
    assert _detect_pii("夏季新款纯棉短袖T恤 男装") == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("联系店主13800138000", "phone"),
        ("客服邮箱 shop@example.com", "email"),
        ("店主身份证11010119900307123X", "id_card"),
        ("sessionid=abcdef1234567890", "cookie"),
        ("token: abc123def456ghi789xyz", "token"),
    ],
)
def test_pii_kinds_are_detected(text, kind):
    """★ 每条先断言**输入里真有那个值** —— 否则下面的断言在输入写错时也是绿的
      （`test_redact` 那套对照纪律：先证有真值，再证被认出来）。"""
    assert _detect_pii(text) == [kind]


def test_the_phone_rule_respects_digit_boundaries():
    """★★ 一对照：同样的 11 位数字，**独立出现**认得出，**埋在长数字里**认不出。

    这正是那条 `(?<!\\d)...(?!\\d)` 边界在干的活：订单号里恰好含一段像手机号的
    数字是常事，把它当成 PII 会让"这行有联系方式"这个信号变成噪声。
    先证真值确实在里面（`in`），空结果才有意义 —— 否则"没认出来"和"输入压根没有"
    长得一模一样。
    """
    order_no = "订单号230917138001380001988"
    assert "13800138000" in order_no  # ← 真值在里面
    assert _detect_pii(order_no) == []  # ← 但仍然不该认出来
    assert _detect_pii("13800138000") == ["phone"]  # ← 独立出现就认得出


def test_detect_pii_returns_kinds_not_counts():
    """★ 返回的是**种类列表**（`sorted(probe.counts)` 取的是键），不是次数。

    两个手机号 + 一个邮箱 → `['email', 'phone']`，不是 `['email', 'phone', 'phone']`
    也不是 `{'phone': 2}`。"有几个"是另一件事（`Redactor.counts` 才是那个），
    这里问的只是"**有没有**哪一类"。混起来会让报告里出现 `pii_in_title:2` 这种
    既不是种类也不是人话的东西。
    """
    assert _detect_pii("13800138000 和 13900139000 和 a@b.com") == ["email", "phone"]


# ── Redactor：正则之下的替换语义 ──────────────────────────
def test_the_longest_literal_is_replaced_first():
    """★ 长值优先。反过来的话先换掉"我的测试小店"，"我的测试小店铺"会剩一个"铺"字
      —— 盘上留下一段半截真值，而且它看起来像"替换成功了一部分"，不像 bug。
      所以这里断的是**没剩下碎片**，不是"替换发生了"。"""
    red = Redactor(literals=["我的测试小店", "我的测试小店铺"])
    out = red.text("我的测试小店铺和我的测试小店")
    assert "铺" not in out
    assert out == "<REDACTED>和<REDACTED>"
    assert red.counts == {"literal": 2}


def test_a_too_short_literal_is_rejected_loudly():
    """★ 报错而不是跳过。跳过是静默的：脱敏照常"成功"，只是日志不可读了。"""
    with pytest.raises(ValueError) as ei:
        Redactor(literals=["小店"])
    assert str(MIN_LITERAL_LEN) in str(ei.value)
    assert "小店" in str(ei.value)  # 报错要说清是哪个词


def test_obj_does_not_redact_dict_keys():
    """★ 只洗值，不洗键 —— 键是**字段名**（`title` / `run_id`），不是秘密。
    洗键会把 JSON 的结构本身改掉，读回来时字段名对不上，比漏一个值严重得多。"""
    got = Redactor(literals=["我的测试小店"]).obj({"我的测试小店": "我的测试小店"})
    assert got == {"我的测试小店": "<REDACTED>"}


def test_nested_structures_are_redacted():
    got = Redactor(literals=["我的测试小店"]).obj({"a": [{"b": "我的测试小店"}]})
    assert got == {"a": [{"b": "<REDACTED>"}]}


def test_extra_patterns_are_counted_as_their_own_kind():
    """派生规则（YAML 的 `redact_extra` 走这条）记在 `extra` 名下 ——
    订单号没有跨平台通用形态，只能由配置方给出精确形态，
    所以它必须能和内置的几类**分开统计**，否则"我加的规则到底有没有命中"查不出来。"""
    red = Redactor(extra_patterns=[r"ORD\d{6}"])
    assert red.text("单号 ORD123456") == "单号 <REDACTED:extra>"
    assert red.counts == {"extra": 1}


def test_non_string_input_passes_through():
    """`""` 和 `None` 原样返回。审计日志里 None 和 "" 是两件事（没探到 vs 探到空），
    这里把它们洗成同一个值就等于抹掉这个区别。"""
    assert Redactor().text("") == ""
    assert Redactor().text(None) is None


# ── suspicious：算出来 → 绑进去 → 读得回 ──────────────────
def test_suspicious_is_zero_when_nothing_is_wrong(tmp_path):
    products, _, _ = _save_and_read(tmp_path, [_row()])
    assert products[0]["suspicious"] == 0
    assert json.loads(products[0]["sanity_flags_json"]) == []


def test_suspicious_is_one_when_a_sanity_flag_fires(tmp_path):
    products, _, _ = _save_and_read(tmp_path, [_row(price=Decimal("0"))])
    assert products[0]["suspicious"] == 1
    assert json.loads(products[0]["sanity_flags_json"]) == ["price<=0"]


def test_suspicious_is_one_when_the_title_carries_a_phone_number(tmp_path):
    """★ 两条来路（sanity 与 PII）都能把 `suspicious` 置 1 —— 它是个 bool，
      不是"哪一类问题"的编码。所以单看这一列无法回答"为什么可疑"，
      要看 `sanity_flags_json`：那条才是人话。"""
    products, _, _ = _save_and_read(tmp_path, [_row(title="联系店主13800138000")])
    assert products[0]["suspicious"] == 1
    assert json.loads(products[0]["sanity_flags_json"]) == ["pii_in_title:phone"]


def test_the_title_is_stored_verbatim_even_when_it_carries_a_phone_number(tmp_path):
    """★★ ADR 11 那条"行数据保真"的实测钉子。

    标题**不做替换式脱敏**，只做检测 + 标记。理由是替换之后库里存的就不是页面上
    真实的值了，而"标题里有没有联系方式"本身是业务信息（不少卖家往标题里塞电话）。
    这条测试就是那个断言的机器可读版本：**真值原样在库里**，同时**标记也在**。
    """
    products, _, _ = _save_and_read(tmp_path, [_row(title="联系店主13800138000")])
    assert products[0]["title"] == "联系店主13800138000"
    assert "REDACTED" not in products[0]["title"]


def test_pii_markers_reach_the_run_level_but_sanity_flags_do_not(tmp_path):
    """★ 一处**名实不符**，钉住当前行为（不是"这是对的"，是"现在是这样"）：

      `runs.sanity_flags_json` 这个字段名听起来像"这次 run 的全部可疑点"，
      但实测它**只装 PII 标记**：上面那行 `price<=0` / `stock<0` 的那一行
      在 run 级聚合里**找不到**（它只进 `products` 表的那一行）。

      依据是代码里那句注释自己划的范围（"能看到这批数据里有几行带联系方式"）——
      意图是清楚的，只是字段名比意图宽。
    """
    rows = [
        _row(goods_id="222", title="联系店主13800138000"),
        _row(goods_id="333", price=Decimal("0")),
    ]
    products, run_row, _ = _save_and_read(tmp_path, rows)

    assert products[0]["suspicious"] == 1 and products[1]["suspicious"] == 1  # 行级：两条都标了
    assert json.loads(run_row["sanity_flags_json"]) == {"222": ["pii_in_title:phone"]}  # 聚合：只有 PII


def test_the_row_level_aggregate_is_keyed_by_goods_id(tmp_path):
    """run 级聚合的键用 goods_id，没有 id 时退化成 `seq-N` —— 注意和
    `all_sanity_flags()` 的 `<row-N>` **不是同一个写法**（一个叫 seq 一个叫 row），
    两处都在生产代码里，这里如实钉住，免得下次以为是笔误随手统一。"""
    _, run_row, _ = _save_and_read(tmp_path, [_row(goods_id="", title="联系13800138000")])
    assert list(json.loads(run_row["sanity_flags_json"])) == ["seq-0"]


def test_duplicate_goods_ids_are_detectable(tmp_path):
    """`(run_id, seq)` 主键的回报：重复行进得来，于是"重复过"这件事查得到。

    若主键是 `(run_id, goods_id)`，重复行会在入库那一刻被静默合并 ——
    "分页重叠 / LLM 重复输出"这个信号就永久消失了。
    """
    _, _, clean = _save_and_read(tmp_path, [_row(goods_id="111"), _row(goods_id="222")])
    assert clean == []

    rows = [_row(goods_id="999"), _row(goods_id="999"), _row(goods_id="888")]
    _, _, dups = _save_and_read(tmp_path, rows, record=_record("2026-01-01T00:00:00+00:00-dup"))
    assert dups == ["999"]


def test_the_run_record_is_redacted_before_it_is_serialized(tmp_path):
    """★ run 记录整体过 `redactor.obj()`，**产品行不过** —— 这两句话必须一起成立，
      只测一半会让人以为"哪里都没洗"或"哪里都洗了"。

      对照：先断言原文里真有那个真值（否则"洗掉了"是空的），再断言落库后没有了。
    """
    literal = "我的测试小店"
    rec = _record(result_raw=f"店铺 {literal} 的采集结果", errors=[f"{literal} 出错了"])
    assert literal in rec.result_raw  # 先证真有

    _, run_row, _ = _save_and_read(
        tmp_path,
        [_row(title=f"来自{literal}的商品")],
        record=rec,
        redactor=Redactor(literals=[literal]),
    )

    assert literal not in run_row["result_raw"]
    assert literal not in run_row["errors_json"]

    # ★ 同一份 redactor 下，产品行的标题仍然是原文 —— 见上面那条 ADR 11 的用例
    products, _, _ = _save_and_read(
        tmp_path,
        [_row(title=f"来自{literal}的商品")],
        record=_record("2026-01-01T00:00:00+00:00-x2"),
        redactor=Redactor(literals=[literal]),
    )
    assert literal in products[0]["title"]


def test_the_redaction_counts_column_comes_from_the_record_not_from_the_redactor(tmp_path):
    """★★ 一个反直觉的形态，钉住（写这个文件时被它绊了一次，第一版断言是错的）：

      上面那次 `save_run(..., redactor=r)` 明明洗掉了 2 处，落库的
      `redaction_counts_json` 却是 **`{}`** —— 因为这一列**不是**由传进来的
      `redactor` 填的，而是由 `record.redaction_counts` 这个字段填的
      （`repository.py` 走 `_j(payload["redaction_counts"])`，payload 来自 record）。

      真实路径上没问题：盖章的是 recorder（`"redaction_counts": dict(self.redactor.counts)`），
      而且 runner 传给 recorder 和 `save_run` 的是**同一个** `self.redactor` 实例
      （`runner.py:1002` 与构造 recorder 处），所以列和实际是同源的。

      钉住它是因为"同一个函数里、同一个概念有两个来源"很容易在下一次改动里分叉：
      谁要是改了 recorder 的盖章逻辑，`save_run` 不会报错也不会变红，
      只会安静地让"这次洗了几处"和"实际洗了几处"说两套话 ——
      又一个"某个通道从没发过它"的家族成员。
    """
    literal = "我的测试小店"
    rec = _record(result_raw=f"店铺 {literal} 的采集结果")

    # ① 真洗了，但没人盖章 → 列是空的
    _, run_row, _ = _save_and_read(tmp_path, [], record=rec, redactor=Redactor(literals=[literal]))
    assert literal not in run_row["result_raw"]  # 脱敏确实发生了
    assert json.loads(run_row["redaction_counts_json"]) == {}  # 可计数列仍是空的

    # ② 盖上章 → 原样落库。★ save_run【不核对】这个数和实际洗了几处是否相符
    stamped = _record(
        "2026-01-01T00:00:00+00:00-y",
        result_raw=f"店铺 {literal} 的采集结果",
        redaction_counts={"literal": 2},
    )
    _, run_row2, _ = _save_and_read(
        tmp_path, [], record=stamped, redactor=Redactor(literals=[literal])
    )
    assert json.loads(run_row2["redaction_counts_json"]) == {"literal": 2}


# ── 授权头的规范形态：这条以前是已知缺口，2026-09-17 修好了 ──────
def test_a_bearer_token_in_an_authorization_header_is_redacted():
    """★ 这条**以前是 `strict xfail`** —— 「已知缺口的机器可读记录」，
    **不等于"这条已经测过了"**。2026-09-17 修好 `redact.py` 之后它转正，
    现在是一条真的守卫。（修的时候它先变成 XPASS 主动失败，这才对。）

    修之前实测（真跑）：

        "Authorization: Bearer abc123def456ghi789xyz"
          → 一个字都没盖住
        "bearer=abc123def456ghi789xyz"
          → 反而这个少见形态认得出

    机制：老规则要求 `[:=]` 出现在关键字**之后**，而规范头部里冒号在
    `Bearer` **之前** —— 那个 `bearer` 分支对它的规范形态永远不命中。
    这正是该模块 docstring 警告过的失效（"最危险的地方在于它看起来在工作"）：
    `token:` / `api_key=` 是好的，于是"凭证类规则在工作"看起来成立。
    """
    secret = "abc123def456ghi789xyz"
    text = f"Authorization: Bearer {secret}"
    assert secret in text  # ★ 先证明输入确实含真值，否则下面那条断言会白过
    assert Redactor().text(text) == "Authorization: Bearer <REDACTED:token>"


def test_the_bearer_rule_does_not_fire_on_prose():
    """★ 对照实验：**这条输入就是「笼统的 bearer + 空白 + 值」会误伤的那一个。**

    把规则改回不锚头名的写法（`\\bbearer\\s+(值)`）→ 这条必红：
    `authentication` 有 14 个字符、又全在字符类里，会被当成凭证盖掉
    （实测产物：`error: Bearer <REDACTED:token> is required`）。

    所以这条钉住的**不是**"能盖住"，是"**没盖过头**"——
    脱敏的第一个失败模式是漏，第二个是滥，两个都要有钉子。
    """
    text = "error: Bearer authentication is required"
    assert Redactor().text(text) == text


def test_the_legacy_bearer_forms_still_work():
    """回归：新增的头名规则不能把已有的 `bearer=` / `bearer: ` 形态挤掉。

    两种形态各走各的规则（一条认 `[:=]`、一条认空白），互不重叠。
    两条路都钉住，免得以后"修一处、坏一处"。
    """
    secret = "abc123def456ghi789xyz"
    assert Redactor().text(f"bearer={secret}") == "bearer=<REDACTED:token>"
    assert Redactor().text(f"bearer: {secret}") == "bearer: <REDACTED:token>"


def test_an_already_redacted_header_is_not_redacted_twice():
    """已脱敏的产物再洗一遍必须原样 —— 否则"洗过了"和"洗出新东西"分不开。

    占位符 `<REDACTED:token>` 里有 `<` `>` `:` 三个不在字符类里的字符，
    `REDACTED` 又只有 8 个字符（< 12），所以头名规则不会二次开火。
    """
    text = "Authorization: Bearer <REDACTED:token>"
    assert Redactor().text(text) == text


def test_a_short_value_after_bearer_is_left_alone():
    """下限与既有规则一致（12 个字符）：短值不盖。"""
    text = "Authorization: Bearer abc"
    assert Redactor().text(text) == text


def test_the_bearer_rule_counts_under_the_token_kind():
    """计数必须并进 `token` 这个 kind —— 报告与看板上它就是"盖了几处凭证"。

    ★ 三种 scheme 走的是**同一条** `_p` 条目，所以计数必然合并；
      下面 `Basic` / `Token` 那几条也各自带了 counts 断言，是同一个理由。
    """
    r = Redactor()
    r.text("Authorization: Bearer abc123def456ghi789xyz")
    assert r.counts == {"token": 1}


# ── 同一族的另外两个实例：Basic / Token（2026-09-18 补）─────────
def test_basic_and_token_scheme_headers_are_redacted():
    """★ 这一族漏着的两条，2026-09-18 补上。

    ⚠️ 补之前**本文件里没有它们的守卫** —— 文件头当时明写"故意没顺手加，
      别以为在这测过了"。也就是说 README 缺口表里那两行「仍一个字都不盖」
      是**文档说了、机制没跟上**，而且是全仓库**唯一**一处。现在两侧对齐。

    三种 scheme 的字母表**真的不同**（理由在 `redact.py` 的注释里）：
    `Basic` 是标准 base64，多 `+ / =`；`Bearer` 是 base64url，多 `. -`。
    """
    cases = {
        # Basic：base64(user:password)，三种典型字母表
        "Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l":
            "Authorization: Basic <REDACTED:token>",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==":
            "Authorization: Basic <REDACTED:token>",
        "Authorization: Basic dXNlcjpwYXNz+dmVy/abc=":
            "Authorization: Basic <REDACTED:token>",
        # DRF TokenAuthentication：40 位十六进制
        "Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b":
            "Authorization: Token <REDACTED:token>",
        # 头名大小写不敏感、`proxy-` 前缀（代理场景下真的会出现）
        "authorization: basic YWxhZGRpbjpvcGVuc2VzYW1l":
            "authorization: basic <REDACTED:token>",
        "Proxy-Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l":
            "Proxy-Authorization: Basic <REDACTED:token>",
    }
    for text, want in cases.items():
        # ★ 先证明输入确实含真值 —— 否则下面那条断言可能只是"本来就没东西可盖"（恒真空转）
        secret = text.split()[-1]
        assert len(secret) >= 12 and secret in text
        assert Redactor().text(text) == want, f"没盖住：{text!r}"

    r = Redactor()
    r.text("Authorization: Basic dXNlcjpwYXNz\nAuthorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b")
    assert r.counts == {"token": 2}, f"两种 scheme 应并进同一个 kind：{r.counts}"


def test_the_scheme_rule_keeps_basic_base64_symbols():
    """★ 这条钉的是**字符类**，不是锚点 —— 也就是这次最容易修错的地方。

    把字符类收回 `Bearer` 那个字母表（去掉 `+ / =`）→ 这条必红。
    它不是"顺手多写几个字符"：`+` `/` `=` 是标准 base64 的**正常输出**，
    而在 `Bearer` 用的 base64url 里这两个符号**根本不会出现**。
    两种编码长得像、字母表不同 —— 这正是本条缺口当初没被顺手修掉的原因。
    """
    secret = "dXNlcjpwYXNz+dmVy/abc="
    text = f"Authorization: Basic {secret}"
    assert secret in text

    out = Redactor().text(text)

    assert out == "Authorization: Basic <REDACTED:token>", (
        f"base64 里的 + / = 让值被截断了，盘上留了半截真凭证：{out!r}"
    )
    # ★ 逐字符复核：值里的每一个符号都不许剩。只盖到 `+` 之前是**最坏**的形态 ——
    #   它看起来像"盖成功了"，实际留下一截可直接使用的凭证。
    for ch in "+/=":
        assert ch not in out, f"{ch!r} 还留在产物里：{out!r}"


def test_the_legacy_token_forms_still_work():
    """回归：新的头名规则不能把已有的 `token=` / `token: ` 形态挤掉。

    与 `test_the_legacy_bearer_forms_still_work` 同一条纪律 —— 两种形态各走各的规则
    （一条认 `[:=]`、一条认头名 + 空白），两条路都要有钉子，免得"修一处、坏一处"。
    ⚠️ DRF 的 `Token` 这个 scheme 名正好和上面那条关键字规则**同名**，
      所以这条回归尤其必要：多 scheme 之后最容易坏的就是它。
    """
    secret = "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"
    assert Redactor().text(f"token={secret}") == "token=<REDACTED:token>"
    assert Redactor().text(f"token: {secret}") == "token: <REDACTED:token>"


def test_the_prose_over_redaction_after_the_header_is_a_known_cost():
    """⚠️ **这条以「已知代价」命名 —— 它断言的是一个不想要的行为。**

    `Authorization: <scheme> authentication is required` 里的 `authentication`
    （14 个字符、全小写字母、全在字符类里）会被当成凭证盖掉。
    锚头名挡不住它：这次**头名是对的**，坐错位置的是值。

    ★ 为什么写成测试，而不是只写进文档：
      · 只写文档 → 它是一处**安静的错误行为**，读代码的人根本看不到；
      · 写成"期望它被盖"的普通用例 → 把错的行为固化成"对的"，更糟；
      · 命名成「已知代价」→ 后人真去修了它，这条会红，然后被迫读上面这段话，
        再决定是删掉这条、还是改规则。**这样红得有信息量。**

    ★ 它**不是本次引入的**：`Authorization: Bearer authentication is required`
      在 2026-09-17 那条规则下就已经命中，这次只是同一形状从 1 个 scheme 扩到 3 个。
      `redact.py` 里还记了一次**想消掉它但实测失败**的尝试（`(?i)` 会把 `[A-Z]`
      变成 `[A-Za-z]`，前瞻判别信号被自己的旗标中和）—— 别再走一遍。
    """
    for scheme in ("Bearer", "Basic", "Token"):
        text = f"Authorization: {scheme} authentication is required"
        assert (
            Redactor().text(text) == f"Authorization: {scheme} <REDACTED:token> is required"
        ), f"残差变了？{scheme} 这条的处理方式与记录不符"

    # ★ 反面同框：**没有头名**的同一句话不许被盖。
    #   这才是"锚头名"真正保住的那半边 —— 两半摆在一起才看得出这是个非对称的取舍，
    #   而不是"规则没写好"。谁哪天把锚点拆松，这里会红。
    bare = "error: Bearer authentication is required"
    assert Redactor().text(bare) == bare
