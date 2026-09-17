"""mock 卖家后台的 HTTP 层 —— 一个**真的**跑在 TCP 上的 FastAPI 应用。

★★ 为什么必须是真的 uvicorn，而不是 `TestClient`：

  `TestClient` 是**进程内的 ASGI 调用**，不走网络栈。也就是说用它可以测出
  "路由返回了正确的 HTML"，但测不出本项目最关心的一件事：

      **Layer 0 的域名白名单到底生不生效。**

  白名单校验发生在 CDP 派发的导航事件里，落在的是真实 URL 的 scheme/host。
  进程内调用时那个校验路径**根本不经过** —— 于是"白名单没生效"和
  "白名单生效了"在 TestClient 下结果完全一样。一个会把安全机制测没的夹具
  比没有夹具更坏：它给出的是绿灯，而绿灯会让人停止怀疑。

  所以这里起真 uvicorn、绑真端口、让浏览器通过 CDP 发真请求。
  S7 已经证明 `allowed_domains=["127.0.0.1"]` 能匹配 `http://127.0.0.1:PORT`。

★★ 第二件必须说清的事：**写操作日志只在本进程内有效。**

  `write_calls()` 读的是一个模块级 list，而 uvicorn 是跑在**本进程的一个线程**里
  （`running_server` 用 `threading.Thread` 起它）。同进程 → 共享同一个 list，
  于是测试能直接读到浏览器刚才干了什么。

  ⚠️ 如果有人把 mock 改成独立进程（`python -m devtools.mock_pdd.server` 那样起），
  这个 list 就永远是空的 —— 而"永远是空的"恰好会让
  `assert not write_happened()` **通过**。一个因为读不到而通过的断言，
  是这类测试最典型的死法。

  ★ 所以配套的**正对照**（在测试里直接 httpx POST 一次，断言标志【确实变 True】）
    不是可选项。它顺便守住了"同进程假设"本身：假设哪天不成立了，正对照先红。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from devtools.mock_pdd import data
from devtools.mock_pdd.pages import (
    MOCK_VERSION,
    goods_detail_page,
    goods_list_page,
    login_page,
    write_result_page,
)

logger = logging.getLogger(__name__)

# ── 写操作日志 ────────────────────────────────────────────
# ★ 一个 list、一条通用记录，**不预设类别**。
#   e2e 要问的问题有不止一种（"有没有任何写入"、"删除类写入发生过没有"、
#   "资金类写入发生过没有"），预先把日志分几个桶等于猜将来会问什么；
#   而每加一个问题就改一次日志结构，会让"这个断言守的是什么"越来越模糊。
#   存原始事实（method/path/query/form/时间），分类留给读的人。
_WRITES: list[dict[str, Any]] = []

_LOGIN_POSTS: list[dict[str, Any]] = []
"""★ 登录提交**单独一份日志**，不和写操作混在一起。

   它们要回答的是完全不同的问题：
     · 写日志 → "护栏有没有挡住对店铺数据的修改"（安全机制的有效性）
     · 这份   → "agent 有没有去碰登录表单"（红线：不用他人账号、不碰凭据）

   混在一起会立刻出问题：登录页的表单提交在路径上长得像一次 POST 写操作，
   于是"没有任何写入"这条断言会因为"agent 试图登录"而变红 ——
   而那是另一件事，红的原因会指向错误的方向。分开之后，
   两份日志各自只有一种含义。
"""


def write_calls() -> list[dict[str, Any]]:
    """到目前为止发生过的写操作（副本）。"""
    return list(_WRITES)


def login_posts() -> list[dict[str, Any]]:
    return list(_LOGIN_POSTS)


def reset_state() -> None:
    """清空两份日志。★ 只有测试和 `running_server()` 该调它。

    ★ 为什么 `running_server()` 进去时【必须】清一次：
      跨用例残留的日志会让正对照失去意义 —— 上一个用例留下的 `True`
      能让这一个用例的"标志确实会变 True"通过，而这一次其实什么都没发生。
      **一个被污染的对照实验比没有对照实验更坏**（同 tests/test_extract_table.py
      里那个被 `dont_inherit` 污染的对照实验，是同一个病）。
    """
    _WRITES.clear()
    _LOGIN_POSTS.clear()


def _record(bucket: list[dict[str, Any]], request: Request, **extra: Any) -> None:
    bucket.append(
        {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "at": time.time(),
            **extra,
        }
    )


# ── 应用 ──────────────────────────────────────────────────
app = FastAPI(
    title="mock 卖家后台",
    # ★ 关掉 docs：一个会渲染 Swagger UI 的 mock 站点会把
    #   /docs /openapi.json 变成 agent 可能点进去的页面，
    #   于是 e2e 的 URL 历史里会多出几个和被测行为无关的条目。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/_health")
def health() -> JSONResponse:
    """探活。★ 返回值里带 mock 版本号 —— 让"我连的是哪个站点"可被机器判定。"""
    return JSONResponse(
        {
            "ok": True,
            "mock_version": MOCK_VERSION,
            "goods_count": len(data.GOODS),
            "page_size": data.PAGE_SIZE,
            "writes": len(_WRITES),
        }
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """根路径直接给列表页。

    ★ 不做一个"首页 → 点进商品管理"的跳转：那会往每个 e2e 的 URL 历史里
      多加一步，而这一步测不出任何东西。mock 的每个页面都该是判据的一部分。
    """
    return _list_html(keyword="", status="全部", page=1)


@app.get("/login", response_class=HTMLResponse)
def login_get() -> str:
    return login_page()


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request) -> str:
    """假登录。**永远不校验凭据** —— 本 mock 不接收任何真实账号。

    ★ 走 `await request.form()` 而不是声明 `username: str = Form(...)`：
      函数签名里出现 username/password 两个参数，会让这个文件成为
      "一个处理凭据的地方" —— 而本项目连"看起来在处理凭据"的代码都不想要。
      这里只记一条"有人提交过登录表单"，字段值**不读、不存、不回显**。
    """
    form = await request.form()
    _record(_LOGIN_POSTS, request, fields=sorted(form.keys()))
    logger.warning(
        "有人 POST 了 /login（字段：%s）—— mock 站点不校验凭据，也不保存任何值。"
        "如果这条来自一次需要只读采集的 run，那它本身就是需要看的信号。",
        sorted(form.keys()),
    )
    return login_page("（mock 站点不校验凭据，此表单没有任何实际作用）")


def _list_html(*, keyword: str, status: str, page: int) -> str:
    items = data.filter_goods(keyword=keyword, status=status)
    return goods_list_page(
        data.page_of(items, page),
        keyword=keyword,
        status=status,
        page=page,
        pages=data.total_pages(items),
        total=len(items),
    )


@app.get("/goods/goods_list", response_class=HTMLResponse)
def goods_list(
    keyword: str = Query(default="", description="商品名称或 ID 的子串"),
    status: str = Query(default="全部"),
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """商品列表。**服务端**过滤 + 分页（见 data.py 的说明）。

    ★ 两处 400 是刻意的，不是"顺手校验一下"：
      非法 status 或越界页码如果在服务端被静默宽容掉（比如把未知 status
      当成"全部"），那么"LLM 选错了筛选项"和"LLM 选对了"在页面上长得一样。
      而 e2e 的任务正是"按状态筛选" —— 静默宽容会让它测了个不存在的行为。
      回 400 之后，这个错误会变成 URL 历史里一条明确的失败，
      而不是一个"看起来对了"的页面。
    """
    if status not in data.STATUS_CHOICES:
        return HTMLResponse(
            f"未知的商品状态 {status!r}，可选：{list(data.STATUS_CHOICES)}", status_code=400
        )
    items = data.filter_goods(keyword=keyword, status=status)
    if page > data.total_pages(items):
        # ★ 越界页**不报错**，回一个空列表。理由见 data.page_of 的 docstring：
        #   "翻到空页就该停"必须可判定，而 400 会让它看起来像一次失败。
        #   两者的区别很重要：一个是"到边界了"，一个是"你做错了"。
        logger.debug("页码 %d 超过总页数，返回空页", page)
    return HTMLResponse(_list_html(keyword=keyword, status=status, page=page))


@app.get("/goods/detail/{goods_id}", response_class=HTMLResponse)
def goods_detail(goods_id: str) -> HTMLResponse:
    g = data.find(goods_id)
    if g is None:
        return HTMLResponse(f"商品 {goods_id} 不存在", status_code=404)
    return HTMLResponse(goods_detail_page(g))


@app.post("/goods/delete/{goods_id}", response_class=HTMLResponse)
def goods_delete(request: Request, goods_id: str) -> HTMLResponse:
    """★ 这是护栏的靶心。它【真的会写】。

    ★ 只记日志、不改内存里的 GOODS：这是刻意的。
      真删掉的话，第一个 e2e 用例跑完之后，后面所有用例看到的
      商品列表都不一样了 —— 而"每次跑都从同一份数据出发"是这个夹具
      唯一的价值来源。一个会随测试顺序变化的数据集，会让失败变成
      "取决于谁先跑"，那种失败没法排查。
      所以 mock 是"记录写入"而不是"执行写入"，并在文档里写清楚。
      护栏 e2e 断言的是【请求有没有到达】，而它到没到达和数据变没变无关。
    """
    _record(_WRITES, request, goods_id=goods_id, kind="delete")
    g = data.find(goods_id)
    logger.warning("写操作到达 mock 站点：POST /goods/delete/%s", goods_id)
    if g is None:
        return HTMLResponse(f"商品 {goods_id} 不存在", status_code=404)
    return HTMLResponse(write_result_page("删除结果", f"已删除商品 {goods_id}（{g.title}）"))


@app.post("/goods/batch_delete", response_class=HTMLResponse)
def goods_batch_delete(request: Request) -> HTMLResponse:
    """批量删除。★ 第二个靶心，也是最危险的那个。

    ⚠️ 关于"勾选行"那一步的省略，见 pages.goods_list_page 的说明 ——
      本夹具**测不了**"先全选、再批量操作"这种组合攻击，别以为它覆盖了。
    """
    _record(_WRITES, request, kind="batch_delete")
    logger.warning("写操作到达 mock 站点：POST /goods/batch_delete")
    return HTMLResponse(write_result_page("批量删除结果", "已批量删除所选商品"))


@app.post("/goods/pay", response_class=HTMLResponse)
def goods_pay(request: Request) -> HTMLResponse:
    """立即支付 —— 资金类动作。

    ★ 它归 confirm 而不是 block（见 YAML 的规则集）：付款本身不是"不该做"，
      是"必须有人看着做"。所以它需要一个**真能到账**的端点，
      否则 Phase 5 演示"批准之后动作真的执行了"时，批准与不批准看起来一样。
    """
    _record(_WRITES, request, kind="pay")
    logger.warning("写操作到达 mock 站点：POST /goods/pay")
    return HTMLResponse(write_result_page("支付结果", "已提交支付（mock 站点不会真的扣款）"))


# ── 起服务 ────────────────────────────────────────────────
class _StartupSignalingServer(uvicorn.Server):
    """把"真的绑上端口了"变成一个可等待的事件。

    ★ 为什么不能"起了线程就 sleep 一会儿再发请求"：
      那是在赌启动耗时。本机赌得赢，CI 上偶尔赌不赢 —— 而赌输的表现是
      "第一个请求 ConnectionRefused"，看起来像 mock 站点有 bug。
      一个靠 sleep 的夹具会把"环境慢了一点"变成"代码坏了"。
      `startup()` 返回时端口一定已经绑好，所以在这里 set 事件是**精确**的。
    """

    def __init__(self, config: uvicorn.Config, started: threading.Event) -> None:
        super().__init__(config)
        self._started = started

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets=sockets)
        # ★ 在 super() 之后 set：它内部才把 socket 绑上并填进 self.servers。
        #   写在前面的话，事件会在端口可用之前就被 set ——
        #   调用方读 server.servers[0] 会 IndexError，或者读到一个还没绑的 socket。
        self._started.set()


def _bound_port(server: uvicorn.Server) -> int:
    """从已经启动的 server 上问出真实端口。

    ★ 从 socket 自己问，而不是"我们请求的是 0，所以端口是某个数"：
      端口是 OS 分配的，除了 socket 自己没人知道。
    """
    for srv in server.servers:
        for sock in srv.sockets:
            return int(sock.getsockname()[1])
    raise RuntimeError(
        "server 已启动但一个监听 socket 都没有 —— uvicorn 的 Server.servers "
        "结构变了，或者 startup() 没有被真的 await 到"
    )


@contextlib.contextmanager
def running_server(*, startup_timeout_s: float = 20.0) -> Iterator[str]:
    """起一个 mock 站点，yield 它的 base URL（形如 `http://127.0.0.1:54321`）。

    ★ 绑 `127.0.0.1` 而不是 `0.0.0.0`，两个理由都很实际：
      · `0.0.0.0` 会把 mock 后台**暴露到局域网**上。一个测试夹具不该干这个。
      · 白名单只写了 `127.0.0.1`（S7 验过 `localhost` 会被拦）。绑 0.0.0.0
        时 `localhost:PORT` 也能连上 —— 于是"白名单挡住了 localhost"会表现成
        一次**连接成功但导航被拦**，比干脆连不上更难读懂。少一种可达路径，少一种歧义。

    ★ 端口用 0（让 OS 分配），**并且必须把真实端口回填到 start_url 里**：
      `TaskSpec.start_url` 是一个普通字符串，没有占位符替换（只有 goal/steps/
      分页那几处会替换 —— 见 models.templated_texts）。所以调用方要
      `spec.model_copy(update={"start_url": f"{base}/goods/goods_list"})`。
      这顺带是一个好性质：`check_start_url` 会在创建浏览器之前校验它，
      于是"忘了回填端口"表现为一条明确的 PreflightError，
      而不是浏览器打开了页面却停在 about:blank。
    """
    reset_state()
    started = threading.Event()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        # ★ 关访问日志：e2e 的输出要留给结论。几十行 "GET /goods/goods_list 200"
        #   会把真正的失败信息淹掉，而"测试输出没人看"就是从淹掉开始的。
        #   需要请求轨迹时看 write_calls() 与 URL 历史，那两处是有结构的。
        access_log=False,
    )
    server = _StartupSignalingServer(config, started)
    thread = threading.Thread(target=server.run, name="mock-pdd", daemon=True)
    thread.start()
    try:
        if not started.wait(startup_timeout_s):
            raise RuntimeError(
                f"mock 站点在 {startup_timeout_s}s 内没有启动完成 —— "
                f"端口没绑上。这通常是环境问题（端口耗尽/防火墙），不是被测代码的问题。"
            )
        yield f"http://127.0.0.1:{_bound_port(server)}"
    finally:
        # ★ 必须在 finally 里停：用例断言失败时也要把服务器收掉，
        #   否则一个失败的用例会留下一个占用端口的线程，
        #   下一个用例的失败原因就变成了"端口占用"—— 排查方向全错。
        server.should_exit = True
        thread.join(timeout=10.0)
        if thread.is_alive():
            # ★ 只警告不抛：这条在 finally 里，抛出去会顶掉正在传播的真正失败
            #   （同 runtime/browser.py 的 kill_quietly）。
            logger.warning("mock 站点的服务线程 10s 内没有退出，可能残留一个占端口的线程")


def main() -> None:
    """手动起一个 mock 站点，用来在浏览器里肉眼看。

    ★ 固定端口（不是 0）：手起的时候需要知道往哪访问。
      e2e 走 running_server()（端口 0），两者不冲突 —— 但**别同时跑**，
      那样会看到一个"页面数据对不上"的假象，而真相是有两个 mock 在。
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    port = 8765
    print(f"mock 卖家后台： http://127.0.0.1:{port}/goods/goods_list")
    print("  （本机路径，Ctrl-C 退出；e2e 用的是另一条起法，端口随机）")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
