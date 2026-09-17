"""mock 卖家后台的**数据**与查询语义 —— 纯函数，不碰 HTTP、不碰浏览器。

★ 为什么把数据从 server.py 里拆出来：
  服务端过滤 + 分页这两件事的**语义**（keyword 怎么匹配、page 从 1 还是 0 开始、
  超出范围返回空还是最后一片）是 e2e 断言的依据。它们混在路由函数里的话，
  就只能靠"起一个服务器、发一个请求"来验 —— 而那条路每验一次都要
  一个真 HTTP 往返，失败时还分不清是语义错了还是服务没起好。
  拆成纯函数之后，"第 2 页有几行"是一个毫秒级的断言。
"""
from __future__ import annotations

from dataclasses import dataclass

PAGE_SIZE = 3
"""每页行数。★ 刻意小到 3：分页逻辑要能被 e2e 真的走一遍。

   一页放 50 行的话，"下一页"那条路径在测试里永远不会被触发，
   而翻页恰恰是这一类任务里最容易出错的地方（页码从 0 还是 1 开始、
   到底之后是禁用还是返回空页）。
"""


@dataclass(frozen=True)
class Goods:
    goods_id: str
    title: str
    price: str
    """★ 刻意存**字符串**（页面上原样那个样子，带 ¥ 和两位小数），
       而不是 Decimal。

       理由：这个 mock 的作用是**模拟页面**，而页面上就是一行文本。
       存成 Decimal 等于在数据层就把"¥59.90 → 59.90"这步清洗做掉了，
       于是被测的那条路径（从页面文本到结构化字段）中间少了一环 ——
       而那一环正是最会出错的地方（见 output_models.clean_price）。"""
    stock: int
    status: str


# ★ 7 件商品 / 每页 3 行 → 3 页（3 + 3 + 1）。
#   刻意不是 3 的整数倍：整除的话"最后一页刚好满"和"最后一页只有一行"
#   这两种形态里的后一种就测不到，而它正是"到底了没有"最容易判错的情况。
GOODS: tuple[Goods, ...] = (
    Goods("100001", "保温杯 316不锈钢 大容量", "¥59.90", 120, "在售中"),
    Goods("100002", "保温杯 便携款 350ml", "¥39.00", 58, "在售中"),
    Goods("100003", "保温杯 儿童款 带吸管", "¥45.50", 0, "已下架"),
    Goods("100004", "玻璃杯 双层隔热", "¥29.90", 340, "在售中"),
    Goods("100005", "玻璃杯 简约直身杯", "¥19.90", 12, "在售中"),
    Goods("100006", "马克杯 陶瓷 带盖勺", "¥35.00", 76, "已下架"),
    Goods("100007", "马克杯 情侣款 一对装", "¥68.00", 204, "在售中"),
)

STATUS_CHOICES: tuple[str, ...] = ("在售中", "已下架", "全部")


def filter_goods(keyword: str = "", status: str = "全部") -> list[Goods]:
    """按关键词 + 状态过滤。**服务端做**，不是让前端过滤。

    ★ 服务端过滤是刻意的：真实卖家后台就是这样（列表可能有几千行，
      不可能全发给浏览器）。而"服务端过滤"意味着**筛选是一次真实请求**，
      于是"点了筛选但页面没变"这类 bug 在测试里是可观测的 ——
      前端过滤的话，页面 DOM 照样会变，两种实现测起来一模一样。
    """
    kw = (keyword or "").strip()
    out = list(GOODS)
    if kw:
        out = [g for g in out if kw in g.title or kw in g.goods_id]
    if status and status != "全部":
        out = [g for g in out if g.status == status]
    return out


def page_of(items: list[Goods], page: int) -> list[Goods]:
    """取第 `page` 页（**从 1 开始**）。

    ★ 页码从 1 开始，和真实后台的显示一致；超出范围返回空列表而不是最后一片。
      后者（"超范围就返回最后一页"）看起来更友好，但它会让"翻到底了还在翻"
      这个循环永远不会结束 —— 而 max_pages 只是一道软闸门，
      真正的护栏应该是"翻到空页就该停"。空列表让那件事可判定。
    """
    if page < 1:
        return []
    start = (page - 1) * PAGE_SIZE
    return items[start : start + PAGE_SIZE]


def total_pages(items: list[Goods]) -> int:
    """总页数。空结果算 1 页 —— 让"没有结果"显示成"第 1 页，共 0 行"，
    而不是"共 0 页"（后者在前端会渲染出一个没有页码的分页条，
    看起来像页面坏了）。"""
    return max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)


def find(goods_id: str) -> Goods | None:
    return next((g for g in GOODS if g.goods_id == goods_id), None)
