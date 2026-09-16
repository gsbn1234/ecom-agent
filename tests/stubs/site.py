"""真浏览器测试的共用夹具：本地小站点 + agent 装配 + 判据工具。

★ 为什么夹具放在 tests/stubs/ 而不是 devtools/：
  spike（devtools/spike_*.py）是【一次性探路】，它的结论写进 docs/spikes.md 之后
  脚本本身就不再跑了。但这里面有几个东西是【长期资产】，每次跑 CI 都要用：
  本地站点、agent 装配、"是不是真 PNG"的判据。
  这些放在 devtools/ 里的话，测试就得反向依赖一个本该扔掉的目录。

★ 为什么 spike 要有一个共用的本地站点，而不是各自去访问真实网站：
  1. 可复现：真实站点的 DOM 会变，判据就变成"有时通过"，等于没有结论。
  2. 零网络依赖：CI 里也能跑。
  3. 【最关键】它让"域名白名单到底生不生效"可被证伪 —— 见下面 allowed/forbidden。

★ 为什么这套夹具值得进 CI（而不是只在本地跑过就算）：
  它验的全是【未文档化的运行时行为】——回调被不被调用、改写生不生效、
  快照会不会被清空、property 存盘后还在不在。这些是 compat.py 静态哨兵
  覆盖不到的那一半：静态哨兵只能证明"签名还在"，证明不了"行为还在"。
"""
from __future__ import annotations

import contextlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent.parent      # tests/
PROJECT_ROOT = TESTS_DIR.parent                          # 仓库根

# ★ 两个都要插，少一个都会在【某种加载顺序下】才炸：
#   PROJECT_ROOT → `from ecom_agent.config import ...`
#   TESTS_DIR    → `from stubs.fake_llm import ...`（stubs 在 tests/ 下面）
#   跑 pytest 时 conftest 碰巧把 tests/ 放进了 sys.path，所以只插 PROJECT_ROOT
#   在 pytest 下【看不出问题】；但 spike 脚本是直接 python devtools/xxx.py 起的，
#   那条路径没有 conftest —— 于是同一份文件在两种入口下行为不同。
#   显式插两个，就不依赖"谁先被 import"了。
for _p in (TESTS_DIR, PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from browser_use import Agent, BrowserSession  # noqa: E402

from ecom_agent.config import CHROME_PATH, HEADLESS  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

# ── 本地小站点 ────────────────────────────────────────────
# ★ 页面刻意做成"类卖家后台"：有搜索框、有表格、有危险按钮。
#   但它【不复制】真实拼多多的 class 名 —— 真实站点的 class 是构建产物 hash，
#   会随发版变；模仿它等于给自己埋一个必然过期的断言。
#   这里测的是我们的 DSL/护栏/观测链路，不是别人的 DOM 稳定性。
_PAGES: dict[str, str] = {
    "/": """<!doctype html><html data-mock-version="1"><head><meta charset="utf-8">
<title>商品管理</title></head><body>
<h1>商品管理</h1>
<input type="text" aria-label="搜索商品" placeholder="输入商品名称">
<button>搜索</button>
<button>筛选</button>
<table><thead><tr><th>商品ID</th><th>标题</th><th>价格</th></tr></thead><tbody>
<tr><td>100001</td><td>保温杯 316不锈钢</td><td>¥59.90</td></tr>
<tr><td>100002</td><td>保温杯 便携款</td><td>¥39.00</td></tr>
<tr><td>100003</td><td>保温杯 儿童款</td><td>¥45.50</td></tr>
</tbody></table>
<button>批量删除</button>
<button>立即支付</button>
<button>编辑</button>
<a href="/page2">第二页</a>
<a href="EXT_BASE/page2">站外入口</a>
</body></html>""",
    "/page2": """<!doctype html><html data-mock-version="1"><head><meta charset="utf-8">
<title>第二页</title></head><body>
<h1>这是第二页</h1>
<p>PAGE2_MARKER</p>
</body></html>""",
    # ★ 探针页：每个元素刻意只填【一种】文本来源，
    #   这样"取值优先级"就是可证伪的 —— 某个元素冒出别的文本，就说明优先级和以为的不同。
    #   真实卖家后台不会长得这么整齐，但真实后台也【没法用来定位 bug】：
    #   一个按钮文本不对时，你分不清是优先级错了还是它本来就没这个属性。
    "/probe": """<!doctype html><html data-mock-version="1"><head><meta charset="utf-8">
<title>元素文本探针</title></head><body>
<h1>元素文本探针</h1>
<input type="text" placeholder="仅占位符">
<input type="text" aria-label="仅aria标签" placeholder="这个占位符不该赢">
<input type="text" title="仅title属性">
<input type="text" value="仅value值" placeholder="这个占位符该输给value">
<img alt="仅alt文本" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">
<button>搜索</button>
<button>批量删除</button>
<button>立即支付</button>
<button>编辑</button>
<div role="button" tabindex="0"><span>嵌套在span里的下架</span></div>
<a href="/page2">第二页</a>
</body></html>""",
}

# ★ 历史别名：devtools/spike_s2_element_text.py 访问的是 /s2。
#   spike 是"我当时怎么知道这条路能走"的证据，不该为了给夹具改个名就去动它 ——
#   改了就等于动了证据，而证据的价值全在于"它一直是那份东西"。
#   所以这里保留旧路径，让 spike 和新测试共用同一份页面（而不是各留一份，
#   那样两边会慢慢漂移，最后"测试测的"和"当年验的"就不是一回事了）。
_PAGES["/s2"] = _PAGES["/probe"]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = _PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        body = body.replace("EXT_BASE", f"http://localhost:{self.server.server_address[1]}")
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        """静音访问日志 —— 测试输出要留给结论。"""


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """★ 吞掉服务器的报错输出。

        浏览器在导航被拦/关闭标签时会把连接掐掉，站点的处理线程于是在
        recv 上抛 ConnectionResetError，socketserver 默认把整段 traceback 打到
        stderr。那是【预期行为】不是故障，但几十行红色 traceback 会把
        真正的结论淹没 —— 而结论是这些测试的全部价值。
        """


@contextlib.contextmanager
def local_site():
    """起一个本地站点，yield 一个带两个主机名的句柄。

    ★★ 同一个服务器、同一个端口，两个主机名：
        http://127.0.0.1:{port}/   ← 「允许」的那个
        http://localhost:{port}/   ← 「禁止」的那个（解析到同一个 127.0.0.1）

      这是"白名单到底生不生效"能被证伪的关键。如果用 example.com 当"站外"，
      那么"被拦住"和"根本连不上"在结果上长得一模一样 ——
      护栏没生效时测试照样"通过"。用同一个可达的服务器、只换主机名字符串，
      就把唯一的变量隔离出来了：能拦住 = 白名单真的在按域名判定。
    """
    srv = _QuietServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield Site(port)
    finally:
        srv.shutdown()
        srv.server_close()


class Site:
    def __init__(self, port: int) -> None:
        self.port = port

    @property
    def allowed(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def forbidden(self) -> str:
        return f"http://localhost:{self.port}"


# ── agent 装配 ────────────────────────────────────────────
def make_session(**kw: Any) -> BrowserSession:
    args: dict[str, Any] = {"headless": HEADLESS}
    if CHROME_PATH:
        args["executable_path"] = CHROME_PATH
    args.update(kw)
    return BrowserSession(**args)


def make_agent(
    llm: FakeLLM,
    task: str = "spike",
    *,
    agent_kw: dict[str, Any] | None = None,
    **browser_kw: Any,
) -> Agent:
    """装配一个 agent，并把 llm.bind(agent) 做掉（★ 不 bind 的桩造不出 action）。

    ★ agent_kw 和 browser_kw 必须分成两个口子，不能混成一个 **kwargs。
      两边有同名字段（use_vision 只在 Agent、headless 只在 BrowserSession），
      混在一起时"我以为设了 use_vision=False"会静默变成
      "给 BrowserSession 传了个它不认识的参数" —— 如果那边不报错，
      测试就会在【错误的配置下】给出结论，而结论看起来完全正常。
    """
    agent = Agent(
        task=task,
        llm=llm,
        browser=make_session(**browser_kw),
        **(agent_kw or {}),
    )
    llm.bind(agent)
    return agent


def make_action(agent: Agent, name: str, **params: Any) -> Any:
    """按库自己的方式造一个 action 实例（用于在回调里【改写】动作）。

    ★ 为什么改写用实例、而 FakeLLM 里用 dict：
      构造 AgentOutput 时字段会被 pydantic 校验（dict → 模型），
      而 `model_output.action = [...]` 是【赋值】，AgentOutput 没开
      validate_assignment，所以赋值不走校验。库自己就是这么干的
      （agent/service.py:1691-1702：造实例 + setattr + 直接赋值）。
      两条路径都不算野路子，但机制不同，所以这里要分开写清楚。

    ⚠️ create_action_model 每次调用都新建一个类，所以这个实例的类型
      和 Agent 的 output_format union 里的同名类是【不同的类】。
      赋值不校验 → 没问题；若哪天想把它塞进 AgentOutput 构造器 → 会炸。
    """
    reg = agent.tools.registry
    if name not in reg.registry.actions:
        raise KeyError(f"不存在的动作 {name!r}；可用：{sorted(reg.registry.actions)}")
    param_model = reg.registry.actions[name].param_model
    cls = reg.create_action_model(include_actions=[name])
    return cls(**{name: param_model(**params)})


# ── 判据工具 ──────────────────────────────────────────────
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def looks_like_png(raw: bytes) -> bool:
    """★ 判"这是不是一张真 PNG"，而不是判"长度大于 0"。

    长度大于 0 是极易满足的：一个被转坏的 base64、一段报错文本、
    甚至一个 0 字节文件都会让它"看起来有截图"。而截图是可观测性的
    最终交付物 —— 图坏了必须在【这一层】发现，
    不能在打开 report.html 时才发现（那时已经不知道是哪一步坏的）。
    """
    return raw[:8] == PNG_MAGIC


def index_blocks(dom_text: str) -> dict[int, str]:
    """从序列化文本里抽出 {元素索引: 该元素在 LLM 眼里对应的那一段}。

    ★★ 一次踩出来的教训：元素的那一段【不只是一行】。
      实测序列化结果是长这样的：

          [25]<button />
          	搜索

      子元素的文本被放到【下一行的缩进】里，而不是塞在标签中间。
      按"一个索引 = 一行"去匹配，会把每个按钮都判成"文本对不上" ——
      而那是解析错了，不是库的行为错了。
      假警报和漏报一样费时间，而且更打击人：它会让你去改一个本来正确的东西。

    ★ 为什么用正则扫 LLM 实际读到的那个字符串，而不是走库的内部结构：
      判据必须建立在"发给 LLM 的那段文本"上。走内部结构取到的可能是
      "生成该字符串的中间态"，那样即使两者不一致，断言也会通过 ——
      而两者不一致恰恰是要发现的东西。
    """
    import re

    blocks: dict[int, str] = {}
    current: int | None = None
    for line in dom_text.splitlines():
        m = re.search(r"\[(\d+)\]", line)
        if m and re.match(r"^\s*(\|SHADOW\([^)]*\)\|)?\s*\[\d+\]", line):
            current = int(m.group(1))
            blocks[current] = line
        elif current is not None:
            blocks[current] += "\n" + line
        # current is None = 索引区之前的文本（页面标题等），不属于任何元素
    return blocks
