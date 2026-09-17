"""mock 卖家后台 —— Phase 4 的 e2e 载体。

分三层，**刻意不合成一个文件**，因为每一层的"怎么验证"完全不同：

    data.py     纯数据 + 查询语义（keyword 怎么匹配、页码从 1 起、越界回空）
                → 毫秒级断言，不需要 HTTP，不需要浏览器
    pages.py    HTML 渲染
                → 纯字符串进出，可以直接断言"这一页里有没有那个按钮"
    server.py   FastAPI 路由 + 写操作日志 + running_server()
                → 需要真端口，但**仍不需要浏览器**（httpx 打它就行）

★ 这三层分开的直接好处是**失败定位**：e2e 挂了的时候，
  "分页语义错了"和"页面没渲染出那个按钮"和"服务器没起来"
  是三个完全不同的排查方向。混在一个文件里的话它们都表现为
  "e2e 红了一行"。

★ 真正需要浏览器的只有一条链（YAML → agent → 真 Chrome → mock → sqlite），
  它在 tests/test_mock_pdd_e2e.py，标 needs_browser。上面三层
  全部可以在没有任何浏览器的环境里跑完 —— 这对 CI 很重要：
  CI 上浏览器起不来是常态，而"起不来"不该让分页语义的测试也一起不跑。

★ 与 tests/stubs/site.py 的关系（为什么不合并）：
  那个是 Phase 2 的**最小探针站点** —— 它的职责是让"白名单生不生效"
  可被证伪（同一服务器两个主机名），页面只有几十行，没有服务端逻辑。
  这个是**仿卖家后台**：真实路由、服务端过滤、分页、可证伪的写操作日志。
  两者服务的问题不同（"库的行为对不对" vs "我们的链路对不对"），
  合并会得到一个又大又慢、且没人说得清它在守什么的夹具。
"""

from devtools.mock_pdd.data import (
    GOODS,
    PAGE_SIZE,
    STATUS_CHOICES,
    Goods,
    filter_goods,
    find,
    page_of,
    total_pages,
)
from devtools.mock_pdd.pages import MOCK_VERSION
from devtools.mock_pdd.server import (
    app,
    login_posts,
    reset_state,
    running_server,
    write_calls,
)

__all__ = [
    "GOODS",
    "MOCK_VERSION",
    "PAGE_SIZE",
    "STATUS_CHOICES",
    "Goods",
    "app",
    "filter_goods",
    "find",
    "login_posts",
    "page_of",
    "reset_state",
    "running_server",
    "total_pages",
    "write_calls",
]
