"""拼多多商品列表的结构化输出模型。

★ 这个模型同时被三方使用，所以它的字段设计要同时满足三方：
  1. LLM —— 通过 output_model_schema 注入，字段描述就是给模型的指令
  2. pydantic —— 校验 LLM 的产出，不合法就触发重试/隔离
  3. SQLite —— 字段直接对应 products 表的列

  三方共用一份定义的好处：不存在"模型改了、表没改"的漂移。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ecom_agent.dsl.registry import register_output_model


class ParseStatus(str, Enum):
    """一次采集的结果状态。对应 runs 表的 parse_status 列。"""

    OK = "ok"
    EMPTY = "empty"
    """合法但零行 —— 可能是筛选条件确实没结果，也可能是选择器没找对。需人工区分。"""

    SCHEMA_INVALID = "schema_invalid"
    """LLM 的产出不符合 schema，且重试后仍不符合 → 整体拒绝入库（quarantine）。"""

    BLOCKED = "blocked"
    """被护栏拦掉了。★ 这种状态【不重试】—— 重试只会再次撞护栏，纯烧 token。"""


# ★ 确定性清洗：把页面上各种价格写法归一成 Decimal。
#   为什么是正则白名单而不是"去掉所有非数字字符"：
#   后者会把 "12.00元/件" 清成 "12.00"（对），但也会把 "满100减20" 清成 "10020"（错，
#   而且错得很像真的）。白名单只认它认识的形态，不认识的原样交给 pydantic 报错 ——
#   报错会触发 LLM 重试，而静默算错不会。
_PRICE_RE = re.compile(r"^\s*[¥￥$]?\s*([0-9][0-9,]*)(?:\.([0-9]{1,2}))?\s*元?\s*$")


def clean_price(v: object) -> object:
    """把 "¥12.00" / "1,299元" / 12.0 归一成 Decimal。不认识的形态原样返回交给校验报错。"""
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if not isinstance(v, str):
        return v
    m = _PRICE_RE.match(v)
    if not m:
        return v  # 交给 pydantic 报错，不猜
    int_part = m.group(1).replace(",", "")
    dec_part = m.group(2) or "0"
    try:
        return Decimal(f"{int_part}.{dec_part}")
    except InvalidOperation:
        return v


class ProductRow(BaseModel):
    """一行商品。

    ★ extra="forbid" 是刻意的。
      允许额外字段意味着 LLM 可以自由发挥，而"自由发挥出来的字段"没有任何人校验过。
      对一个卖家要拿去做补货决策的数据来说，宁可整体失败（quarantine，raw 原文留着），
      也不要一个"多了几个来源不明的字段"的成功结果。
      代价是 LLM 偶尔多写一个字段就要重试 —— 这个代价是值的，而且是可观测的。
    """

    model_config = ConfigDict(extra="forbid")

    goods_id: str = Field(description="商品ID，页面上的一串数字，原样抄写不要改写")
    title: str = Field(description="商品标题，原样抄写")
    price: Decimal = Field(description="售价，只填数字，不要带货币符号")
    stock: int = Field(description="库存件数，整数")
    status: str = Field(description="商品状态，如 在售中 / 已下架")

    @field_validator("price", mode="before")
    @classmethod
    def _normalize_price(cls, v: object) -> object:
        return clean_price(v)

    def sanity_flags(self) -> list[str]:
        """返回可疑点的列表。空列表 = 通过。

        ★ 标记而【不删除】。
          删掉之后，"LLM 出错的方式"就再也看不到了 —— 而那恰恰是接下来最该看的东西：
          它决定了是调提示词、还是加确定性清洗、还是这个字段根本采不到。
          删除会把一个可诊断的信号变成一个沉默的空洞。
        """
        flags: list[str] = []
        if self.price <= 0:
            flags.append("price<=0")
        if self.stock < 0:
            flags.append("stock<0")
        if not self.title.strip():
            flags.append("title_empty")
        if not self.goods_id.strip().isdigit():
            flags.append("goods_id_not_numeric")
        # 明显不合理的量级：库存 10 万件以上多半是把"销量"填进了"库存"
        if self.stock > 100_000:
            flags.append("stock_implausibly_large")
        return flags


@register_output_model("pdd.ProductRowList", version=1)
class ProductRowList(BaseModel):
    """一次采集的完整输出。这是 Agent 的 output_model_schema。"""

    model_config = ConfigDict(extra="forbid")

    rows: list[ProductRow] = Field(default_factory=list)
    keyword: str = Field(default="", description="本次搜索用的关键词，原样回填")
    note: str = Field(
        default="",
        description="若未能采集到数据，在这里说明原因（如需要登录、页面结构不符预期）；采到则留空",
    )

    def all_sanity_flags(self) -> dict[str, list[str]]:
        """{goods_id: [可疑点]}。只包含有问题行的条目。"""
        out: dict[str, list[str]] = {}
        for r in self.rows:
            f = r.sanity_flags()
            if f:
                out[r.goods_id or f"<row-{len(out)}>"] = f
        return out
