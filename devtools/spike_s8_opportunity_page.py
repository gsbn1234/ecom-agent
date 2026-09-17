"""S8：真站点「机会商品 / 平台推荐商品」页 —— 只读探路：空店的后台里到底有没有可采的行。

★★ 这个 spike 要回答的是**一个具体的缺口**，不是一个泛泛的好奇：

   Phase 6 的验收句里有一条是「sqlite 有 5 行真实商品」。这条在当前账号上
   **不可达**，而且与我们代码无关 —— 那是个刚入驻的空店，后台七个标签全是 0，
   表格显示"没有查询到符合要求的商品数据"。于是"同一套 DSL / 护栏 / 提取 / 落库
   在**真站点**上采到了行"这件事，目前只有 mock 站点的证据，**真站点侧是空的**。

   计划文件里记过一个候选：**机会商品 / 平台推荐商品** —— 平台推荐的商品来自
   全网数据，理论上**空店里也有行**。⚠️ 但那是**推断，不是实测**：
   我只在后台导航里见过这个入口，DOM 没探过、里面有没有行也没验过。
   所以这个 spike 的第一件事就是**把那个"推断"变成"实测"** ——
   成或不成，两种结果都是有价值的产出（不成 → 那条缺口按"本账号不可验证"
   如实记档，而不是糊一个绿勾）。

★★ 两段式：先**发现**入口，再**采集**那一页。刻意不写死 URL：

   写死一个我猜的 URL，成功了也不知道是不是碰巧，失败了也分不清是"猜错 URL"
   还是"页面真是空的" —— 两种失败长得一样。
   改成"从后台首页的 DOM 里把导航链接读出来"，那么"找不到入口"和"入口后面是空的"
   就是两条不同的、各自可读的结论。

★★ 只读纪律（这个脚本的安全姿态全在这里）：

   · 浏览器动作只有三样：`navigate_to`（两次）、页内 `evaluate` 读 DOM、截图。
     **零点击、零输入、零滚动** —— 点击才是护栏要审的东西，这里根本没有点击。
   · 截图落在 `runs/`（已在 .gitignore）—— 真实后台画面**不进仓库**。
     写文档时只记结构与行数，**不抄商品内容**（README 已声明"仓库里不含真实店铺数据"）。
   · 不翻页、不重试（计划 R2：真站点失败不恋战）。读表 max_rows=5，停留时间越短越好。

★★ 采行用的是**项目自己的** `extract_table_impl`，不是这里手写一段 JS：

   手写 JS 读出来的行只能证明"页面上有字"，证明不了"我们的采集器能读到它" ——
   而后者才是那条验收真正缺的证据。所以诊断（统计有多少表、多少行）用手写 JS，
   **证据**走 `extract_table_impl`，与真 run 里 LLM 调的是同一个函数。

★★ 为什么不用 `runtime/browser.py` 的 `browser_session()` 上下文管理器：

   它的 `finally` 里是**强杀**（`kill_quietly`）。而真站点脚本必须让 cookie 落盘 ——
   实测（见 `close_gracefully_and_flush` 的 docstring）：强杀会丢掉那些"还在内存里"
   的 cookie，于是下一次 run 落在登录页、**静默零行**。
   所以这里照 `devtools/login_pdd.py` 的 `open_session` 那样自己装配、自己收尾：
   `build_browser_session()`（白名单照旧生效，那就是 Layer 0）+ `finally` 里优雅关闭。

跑法：`uv run python devtools/spike_s8_opportunity_page.py`
（要 `.env` 里的 `ECOM_AGENT_USER_DATA_DIR` 指向登录过的 profile；
 落在登录页就停下来 —— 那时只有你能扫码，脚本不会替你做任何登录动作。）
"""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from spike_lib import run_spike  # noqa: E402

from ecom_agent.actions.extract_table import (  # noqa: E402
    ExtractTableAction,
    extract_table_impl,
)
from ecom_agent.config import (  # noqa: E402
    CHROME_PATH,
    HEADLESS,
    RUNS_DIR,
    TASKS_DIR,
    USER_DATA_DIR,
)
from ecom_agent.dsl.compiler import compile_task  # noqa: E402
from ecom_agent.dsl.loader import load_task  # noqa: E402
from ecom_agent.runtime.browser import (  # noqa: E402
    build_browser_session,
    close_gracefully_and_flush,
)
from ecom_agent.runtime.loginstate import LOGGED_IN, probe  # noqa: E402

TAG = "S8"

PDD_HOME = "https://mms.pinduoduo.com/"

# ★ 编译用的模板只为了拿到**白名单 + 持久 profile**（这两样来自 YAML 与 --profile）。
#   本脚本不去执行那个任务的步骤 —— 它自己做自己的三次读操作。
SPEC_PATH = TASKS_DIR / "pdd_shop_overview.yaml"

# 入口关键词，**有顺序**：越靠前越像"平台推荐的商品"。
#   ★ 写成有序列表而不是一个正则：命中哪个词本身就是要报告的结论之一
#     （"是「机会商品」还是别的什么入口"决定了这个模板该怎么写）。
WANTED = ("机会商品", "平台推荐", "商机", "选品", "推荐商品", "潜力商品")

# 读表只读这么几行：验收要的是"真站点采到了行"，不是"采全了这一页"。
# 停留时间越短，被风控看见的机会越小（R2）。
SAMPLE_ROWS = 5

# 每次导航之后等页面渲染。这是一次性等待，**不是轮询** ——
# 轮询会变成"每隔几秒请求一次真站点"，那是真会招风控的（见 login_pdd.py 的说明）。
SETTLE_S = 5.0

# ── 页内脚本 ──────────────────────────────────────────────
# ⚠️ 这两个脚本都走 `page.evaluate`，而那个方法的返回值契约是反的：
#    它**永远返回字符串**（对象走 json.dumps）。见 extract_table.py 的
#    _decode_table_payload docstring —— 那里记了这个坑的完整经过。

_DISCOVER_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const seen = new Set();
  const links = [];
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const text = norm(a.innerText || a.textContent);
    const href = a.href || '';
    // a.href 是**解析过的绝对地址**（不是 getAttribute 的原文），相对链接到这儿已经补全。
    if (!text || !/^https?:/i.test(href)) continue;
    const key = text + '|' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({text, href});
  }
  return {
    url: location.href,
    title: document.title,
    totalAnchors: document.querySelectorAll('a[href]').length,
    bodyTextLen: norm(document.body ? document.body.innerText : '').length,
    links: links,
  };
}
"""

_CENSUS_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const tables = Array.from(document.querySelectorAll('table')).map((t, i) => {
    const headRows = t.tHead ? Array.from(t.tHead.rows) : [];
    let bodyRows = 0;
    if (t.tBodies && t.tBodies.length) {
      for (const tb of Array.from(t.tBodies)) bodyRows += tb.rows.length;
    }
    return {
      index: i,
      rows: bodyRows,
      headers: headRows.length
        ? Array.from(headRows[headRows.length - 1].cells).map((c) => norm(c.innerText))
        : [],
    };
  });

  // ★ 通用"重复块"探测：真实后台常把列表渲染成 div 网格，不用 <table>。
  //   不猜 class 名（那是构建产物 hash，会随发版变），而是找**结构**：
  //   同一个父节点下 ≥3 个"同标签 + 同 class"的兄弟 = 一个列表。
  //   这条判据对 class 名完全不敏感，所以它对改版是稳的。
  // ★ 只扫**会渲染的**元素。第一版忘了滤掉 SCRIPT，于是"最大的重复块"是
  //   30 个同形 <script>（行内埋点脚本）—— 一个荒唐但真实的结果：
  //   探测器忠实地回答了问题，只是那个问题问错了对象。
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
      if (kids.length < 3) continue;
      const sample = norm(kids[0].innerText);
      // 成员文本太长的多半是布局容器而不是列表项；太空的多半是装饰元素。
      if (!sample || sample.length > 300) continue;
      groups.push({
        tag: key.split('|')[0],
        count: kids.length,
        sample: sample.slice(0, 160),
        imgs: kids[0].querySelectorAll('img').length,
        // ★★ 成员**未折叠**的 innerText 逐行拆开 —— 这才是"确定性采集器"要吃的东西：
        //   卡片里的"标题 / ¥价格 / 热度 N / 按钮文字"在 DOM 里就是几行文本，
        //   读完这几行就知道"按行取字段"这条设计成不成立。第一版只给了折叠后的
        //   一行（`norm()` 把换行压成空格），于是"标题和价格是不是同一行"这个
        //   决定 reader 怎么写的问题，在证据里**没法回答** —— 只能再跑一次真站点。
        lines: (kids[0].innerText || '')
          .split('\n').map((s) => s.replace(/\s+/g, ' ').trim()).filter((s) => s),
      });
    }
  }
  groups.sort((a, b) => b.count - a.count);

  return {
    url: location.href,
    title: document.title,
    bodyTextLen: norm(document.body ? document.body.innerText : '').length,
    tables,
    groups: groups.slice(0, 12),
  };
}
"""


def _decode(raw: Any) -> dict[str, Any] | None:
    """把 `page.evaluate` 的返回值解成 dict。读不懂返回 None（不猜）。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def _read_dom(session: Any, js: str, *, what: str) -> dict[str, Any]:
    """按 `extract_table_impl` 的同一条路取一次页内数据。"""
    page = await session.get_current_page()
    assert page is not None, f"没有可用页面，读不到{what}"
    payload = _decode(await page.evaluate(js))
    assert payload is not None, f"{what}的页内脚本返回值读不懂（不是 JSON 对象）"
    return payload


def _pick_entry(links: list[dict[str, str]]) -> tuple[str, str, str] | None:
    """从页面上的链接里挑出机会商品的入口。返回 (关键词, 文本, href) 或 None。"""
    for word in WANTED:
        for link in links:
            if word in link["text"]:
                return word, link["text"], link["href"]
    return None


async def body() -> None:
    profile = Path(USER_DATA_DIR).expanduser().resolve() if USER_DATA_DIR else None

    print("=" * 74)
    print(f"profile：{profile or '（.env 里没配 ECOM_AGENT_USER_DATA_DIR → 临时 profile，必然在登录页）'}")
    print(f"headless={HEADLESS}   chrome={CHROME_PATH or '(库自己探测)'}")
    print("=" * 74)

    spec = load_task(SPEC_PATH)
    compiled = compile_task(
        spec,
        chrome_path=CHROME_PATH,
        headless=HEADLESS,
        user_data_dir=USER_DATA_DIR,
    )
    # ★ 白名单来自模板，Layer 0 照旧生效。报告出来是为了让"能到哪儿"这件事
    #   在输出里可见 —— 待会儿导航失败时要能一眼看出是不是被白名单拦的。
    print(f"allowed_domains = {compiled.browser_kwargs.get('allowed_domains')}")

    session = build_browser_session(compiled)
    shot_dir = RUNS_DIR / f"spike_s8_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    shot_dir.mkdir(parents=True, exist_ok=True)
    print(f"证据目录（已 gitignore，不进仓库）：{shot_dir}")

    try:
        # ── 起会话 + 到后台首页 ──
        t0 = time.monotonic()
        await session.start()
        await session.navigate_to(PDD_HOME)
        await asyncio.sleep(SETTLE_S)
        print(f"\n① 已到后台首页（{time.monotonic() - t0:.1f}s）")

        # ── 先判登录态。落在登录页就**到此为止**：接下来只有人能做的事 ──
        state = await probe(session)
        print(f"   登录态判定：{state.describe()}")
        assert state.verdict == LOGGED_IN, (
            f"登录态是 {state.verdict!r}（{state.reason}），不是 logged_in —— "
            "停在登录页的话后面全是白跑。先重跑 devtools/login_pdd.py 扫码再回来。"
        )

        await session.take_screenshot(path=str(shot_dir / "01_home.png"))

        # ── ② 发现入口：从 DOM 里读导航链接 ──
        home = await _read_dom(session, _DISCOVER_JS, what="后台首页")
        links = home.get("links") or []
        print(f"\n② 首页 DOM：title={home.get('title')!r} "
              f"链接 {len(links)} 条 / 锚点 {home.get('totalAnchors')} 个 / "
              f"正文 {home.get('bodyTextLen')} 字")
        assert links, (
            "后台首页里一条 <a href> 的导航链接都没读到 —— "
            "要么页面还没渲染完（把 SETTLE_S 调大），要么这个后台的导航不是 <a> 元素"
            "（那 discovery 这条路要换写法）。"
        )

        hit = _pick_entry(links)
        if hit is None:
            # ★ 找不到入口时把**读到的链接**打出来 —— 否则"没找到"是一句无法行动的话。
            print("   ⚠️ 没匹配到机会商品类入口。首页上读到的链接（最多 40 条）：")
            for link in links[:40]:
                print(f"      {link['text'][:40]!r} -> {link['href'][:110]}")
        assert hit is not None, (
            f"首页导航里没有匹配 {WANTED} 任一关键词的链接 —— "
            "见上面打出来的链接清单（可能入口叫别的名字，或不在首页导航里）。"
        )
        word, text, target = hit
        print(f"   命中关键词「{word}」：{text!r}\n   -> {target}")

        # ── ③ 导航到那一页，做结构普查 ──
        try:
            await session.navigate_to(target)
        except Exception as exc:  # noqa: BLE001 —— 要把"被白名单拦"和"导航炸了"分开说
            raise AssertionError(
                f"导航到 {target} 失败（{type(exc).__name__}: {exc}）。"
                f"★ 先看域名在不在白名单 {compiled.browser_kwargs.get('allowed_domains')} 里 —— "
                "不在的话这是 Layer 0 正常工作，模板要改白名单，不是 bug。"
            ) from exc
        await asyncio.sleep(SETTLE_S)
        await session.take_screenshot(path=str(shot_dir / "02_target.png"))

        census = await _read_dom(session, _CENSUS_JS, what="机会商品页")
        print(f"\n③ 目标页：title={census.get('title')!r} "
              f"正文 {census.get('bodyTextLen')} 字\n   url={census.get('url')}")
        tables = census.get("tables") or []
        print(f"   <table> 数：{len(tables)}")
        for t in tables:
            print(f"      [{t['index']}] {t['rows']} 行  表头={t['headers'][:8]}")
        groups = census.get("groups") or []
        print(f"   重复块（≥3 个同形兄弟，按个数排）前 {len(groups)} 组：")
        for g in groups:
            print(f"      {g['count']:>3} 个 <{g['tag']}> ×{g['imgs']} 图 | {g['sample'][:80]!r}")
            if g.get("lines"):
                print(f"          逐行：{g['lines']}")

        # ── ④ 证据：用**项目自己的**采集器读表 ──
        best = max(tables, key=lambda t: t["rows"], default=None)
        rows: list[list[str]] = []
        headers: list[str] = []
        if best and best["rows"] > 0:
            result = await extract_table_impl(
                ExtractTableAction(table_index=best["index"], max_rows=SAMPLE_ROWS),
                session,
            )
            if result.error:
                print(f"\n④ extract_table 报错：{result.error}")
            else:
                data = json.loads(result.extracted_content)
                headers = data["headers"]
                rows = data["rows"]
                print(f"\n④ extract_table 读到 {len(rows)} 行 / 共 {data['total_rows']} 行")
                print(f"   表头：{headers}")
                # ★ 只打**一行**、且截断：够判断"这是真数据"，又不把店铺内容成片倒出来。
                if rows:
                    print(f"   第一行（截断）：{[c[:24] for c in rows[0]]}")
        else:
            print("\n④ 页面上没有带行的 <table>，跳过 extract_table（见上面的重复块清单）")

        # ★★ 落盘探路证据，让**下一轮迭代不必再跑真站点**（R2：真站点足迹越小越好）。
        #   ⚠️ 这两份 JSON 里是真实后台内容（商品标题/价格），所以只能待在 runs/ 里
        #      （已 gitignore）—— **不要**把它们复制进 docs/ 或提交。
        (shot_dir / "discover.json").write_text(
            json.dumps(home, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (shot_dir / "census.json").write_text(
            json.dumps(census, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n截图与 DOM 证据：{shot_dir}（已 gitignore）")

        # ── ⑤ 判定：**三种**结果，不能混成一种 ──
        #   ★★ 第一版这里只有一条 assert，消息写的是"机会商品页没有采到任何一行" ——
        #      而那一页明明有 35 张商品卡。消息是**假的**，方向也是错的：
        #      它会让人去查"这页怎么是空的"，而真相是"这页很满，是我们的采集器读不到"。
        #      这正是本项目反复猎杀的那类缺陷（断言说了句不成立的话），所以拆成三岔。
        if rows:
            print("\n✅ 真站点采到了行（用的是项目自己的 extract_table）")
            return

        cards = [g for g in groups if g["count"] >= 3]
        if cards:
            top = cards[0]
            raise AssertionError(
                f"这页**不空**：有 {len(cards)} 组同形重复块，最大一组是 {top['count']} 个 "
                f"<{top['tag']}>（×{top['imgs']} 图，示例 {top['sample'][:40]!r}）—— "
                "但一个带行的 <table> 都没有。"
                "★ 结论是「我们的 extract_table 读不到它」（它只读 <table>），"
                "不是「页面是空的」。这就是模板之外还差的那件东西。"
            )
        raise AssertionError(
            "这一页既没有带行的 <table>，也没有任何 ≥3 的同形重复块 —— "
            "那它是真的空（不是采集器的问题）。这条缺口按『本账号不可验证』记档。"
        )
    finally:
        # ★ 优雅关闭：让 cookie 落盘。强杀会丢掉还在内存里的 cookie，
        #   下一次 run 就会落在登录页、静默零行（见 close_gracefully_and_flush）。
        await close_gracefully_and_flush(session)


def main() -> int:
    # 库自己的日志压到 WARNING：它在 INFO 级会把整棵 DOM 打出来，把我们这几行人话淹掉。
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("browser_use", "BrowserSession", "utils"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return run_spike(TAG, body)


if __name__ == "__main__":
    sys.exit(main())
