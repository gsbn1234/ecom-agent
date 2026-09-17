"""`extract_cards` 的**页内脚本**在真 DOM 上的行为 —— 离线单测测不了的那一半。

★★ 为什么必须有这个文件，而不是"单测 27 条全绿就够了"：

  `tests/test_extract_cards.py` 里的假 page 只把我喂进去的 payload 原样吐回来，
  所以它证明的是**拿到 payload 之后**的逻辑：字段判据、missing 计数、三种失败形态。
  而 payload 本身是怎么从 DOM 里算出来的 ——

    · "同一父节点下 ≥N 个同形兄弟 = 一个列表"这条判据在真 DOM 上灵不灵；
    · `innerText` 到底怎么分行（字段判据是**按行**匹配的，分行错了全盘皆输）；
    · SKIP 那串黑名单有没有真的把不渲染的标签挡在候选之外；

  ——一个字都没被验到。**假 page 的绿不能代表脚本对**，因为它压根没执行过脚本。

★★ 本文件的对照组是"页面上那 6 个 `<script>`"（见 stubs/site.py 的 /cards 页面）：

  它们故意比商品卡（4 张）**还多**。没有 SKIP 过滤时，成员数最多的组就是它们，
  于是 `group_index=0` 会指到一堆埋点脚本 —— S8 探路在真站点上撞到的原样形态。
  所以 `pickedTag == "DIV"` / `groupCount == 2` 这两条断言
  **只在过滤真的生效时成立**：把 SKIP 里的 'SCRIPT' 去掉，它们当场变红。
  这一点已用手工证伪确认过（**4 条全红** —— 主判据那条依赖同一个机制，不是只有
  对照组两条红；改一处 → 跑 → 还原 → md5 复核。那个临时脚本跑完即弃，落在
  gitignore 的 `runs/` 下，不需要去找它）。

★ 本文件**不**证明什么（说清楚，免得被读成它证明了更多）：
  YAML 的 `card_fields` → 编译 → `build_tools(card_fields=...)` → runner 这条接线
  不在这里。那是 `test_extract_cards.py` 的启动门禁三条用例在管
  （其中一条专门抓"闭包没接上"），YAML→浏览器→sqlite 的整条链路由
  `test_mock_pdd_e2e.py` 的表格版本管。这里只钉页内脚本。

★ 为什么每条用例各开一个会话（而不是共用）：会话是这几条用例里**唯一的重资产**，
  共用能省几秒，代价是一处失败会污染后面所有条 —— 而失败信号的定位价值
  正是这个文件存在的理由（与 test_browser_contract.py 同一取舍）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _p in (TESTS_DIR, PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from browser_use import ActionResult, BrowserSession  # noqa: E402

import ecom_agent.actions.extract_cards as M  # noqa: E402
from ecom_agent.actions.extract_cards import (  # noqa: E402
    ExtractCardsAction,
    extract_cards_impl,
)
from ecom_agent.actions.extract_table import decode_eval_payload  # noqa: E402
from ecom_agent.dsl.models import CardField  # noqa: E402
from ecom_agent.runtime.browser import kill_quietly  # noqa: E402
from stubs.site import Site, local_site, make_session  # noqa: E402

pytestmark = pytest.mark.needs_browser

# 页面是静态的，但 innerText 要等一次布局才准。给一点余量而不是赌 0 ——
# 赌输的表现是"偶尔少一行"，而那种失败最难查（重跑就绿）。
SETTLE_S = 1.0

# ★ 与 S8 在真站点上看到的卡片同形：标题 / ¥价格 / 热度 N / 按钮，四行一张。
#   判据刻意写成**行首锚定**（^），因为"哪一行算价格"必须由行本身决定，
#   不能靠"它在第几行"——真实页面的行数会变（加一行活动标签就错位了）。
FIELDS = [
    CardField(name="title", pattern=r"^(?!¥|热度|发布同款).+"),
    CardField(name="price", pattern=r"^¥\s*([\d.]+)"),
    CardField(name="heat", pattern=r"热度\s*(\d+)"),
]

# 与 /cards 页面一一对应（页面在 stubs/site.py，改页面必须改这里）。
EXPECTED_ROWS = [
    {"title": "测试商品甲", "price": "12.30", "heat": "88"},
    {"title": "测试商品乙", "price": "8.80", "heat": "12"},
    {"title": "测试商品丙", "price": "105.00", "heat": "7"},
    {"title": "测试商品丁", "price": "3.50", "heat": "201"},
]


@pytest.fixture
def site():
    """本地小站点（两个主机名指向同一个服务器，见 stubs/site.py）。"""
    with local_site() as s:
        yield s


async def _open(site: Site) -> BrowserSession:
    """开会话 → 导航到卡片页。调用方负责 kill。

    ★ 带上 `allowed_domains=["127.0.0.1"]`：这条唯一的目的是让测试环境和
      **真 run 的环境**一致（每个 run 都挂着白名单，见 compiler 的 browser_kwargs）。
      白名单本身的行为由 test_browser_contract.py 的 S3/S7 两条管，不在这里重复验。

    ★ keep_alive=False：这几条用例只读 DOM，不需要 cookie 落盘
      （"cookie 要活到下一个会话"是 test_profile_persistence.py 的问题，
      它必须优雅关闭 —— 硬 kill 会丢 cookie，见 runtime/browser.py）。
    """
    session = make_session(keep_alive=False, allowed_domains=["127.0.0.1"])
    await session.start()
    await session.navigate_to(site.cards)
    await asyncio.sleep(SETTLE_S)
    return session


async def _read(site: Site, **params: Any) -> ActionResult:
    """走**实现本体**读一次卡片页（不经过 LLM，也不需要 Tools 注册）。"""
    session = await _open(site)
    try:
        return await extract_cards_impl(
            ExtractCardsAction(**params), session, fields=FIELDS
        )
    finally:
        await kill_quietly(session)


# ══════════════════════════════════════════════════════════
# 主判据：真 DOM 上的卡片被读成了行
# ══════════════════════════════════════════════════════════
async def test_the_card_grid_is_read_into_rows(site: Site) -> None:
    """4 张卡 → 4 行 × 3 字段，值来自页面原文，未经过任何模型转录。

    ★ 顺带钉住的两件事：
      · `group_tag == "DIV"` —— 页面上有 6 个 `<script>`，比卡还多（见本文件顶部）；
        这条断言是 SKIP 过滤的判据，不是装饰。
      · `group_members == 4` —— 选中组的成员数，证明选的是商品卡那一组。
    """
    result = await _read(site)
    assert not result.error, f"读卡片失败：{result.error}"

    data = json.loads(result.extracted_content)
    assert data["group_tag"] == "DIV", (
        f"选中的组不是商品卡（是 <{data['group_tag']}>）—— "
        f"最可能的原因是 SKIP 过滤失效，于是 6 个埋点 <script> 当上了最大的重复块"
        f"（S8 在真站点上撞到的原样形态）。inventory={data.get('inventory')}"
    )
    assert data["group_members"] == 4
    assert data["rows"] == EXPECTED_ROWS
    assert data["missing"] == {"title": 0, "price": 0, "heat": 0}
    assert data["truncated"] is False and data["total_cards"] == 4
    # ★ 摘要里要有"未经模型转录"这句：读产物的人据此判断这行数能不能直接采信。
    assert "未经模型转录" in result.long_term_memory


# ══════════════════════════════════════════════════════════
# 对照组：不渲染的标签连候选都进不去
# ══════════════════════════════════════════════════════════
async def test_non_rendered_tags_never_enter_the_candidate_list(site: Site) -> None:
    """★★ 直接问页内脚本一次：候选组清单里有没有 SCRIPT？

    ★ 为什么这条要绕过实现本体、直接调 `_CARDS_JS`：
      "没被选中"和"没进候选"是两件事，而实现本体的成功产物里**只有被选中的那一组**
      （inventory 只在失败时才出现）。要断言"根本没进候选"，就得看脚本的原始返回值。

    ★ 这是本文件里唯一能证伪 SKIP 的断言：把 'SCRIPT' 从 SKIP 里去掉，
      `groupCount` 会从 2 变成 3、`inventory[0]` 会变成 6 个 <SCRIPT> —— 当场红。
      而主判据那条（pickedTag == "DIV"）在那种情况下也会红，两条是同一机制的两个面。
    """
    session = await _open(site)
    try:
        page = await session.get_current_page()
        assert page is not None, "会话里没有页面"
        raw = await page.evaluate(
            M._CARDS_JS,
            {
                "groupIndex": 0,
                "minMembers": 3,
                "maxCards": 50,
                "maxMemberChars": M._MAX_MEMBER_CHARS,
            },
        )
    finally:
        await kill_quietly(session)

    payload = decode_eval_payload(raw)
    assert payload is not None, f"脚本返回值读不懂：{raw!r}"

    # 页面上的重复块只有两组：4 张商品卡、3 个导航链接。
    # 6 个 <script> 如果没被跳过，这里会是 3 组且最强的是 SCRIPT。
    assert payload["groupCount"] == 2, (
        f"候选组数不是 2（是 {payload['groupCount']}）—— "
        f"多半是 SKIP 没滤掉那 6 个 <script>。inventory={payload['inventory']}"
    )
    assert [g["tag"] for g in payload["inventory"]] == ["DIV", "A"], payload["inventory"]
    assert not any(g["tag"] == "SCRIPT" for g in payload["inventory"])


# ══════════════════════════════════════════════════════════
# 抓错组：可诊断、可纠正
# ══════════════════════════════════════════════════════════
async def test_a_wrong_group_index_gives_diagnosable_rows(site: Site) -> None:
    """`group_index=1` 读到的是导航条（3 个 `<a>`）—— 而且**看得出来**读错了。

    ★ 这条钉的不是"能读第二组"，而是**读错之后会发生什么**：
      3 行里 title 有值（"首页"等），price / heat 全空且 missing 计数是 3。
      于是"判据在这组上匹配不上"这件事是**可见的** —— LLM（和人）据此知道
      该换个 group_index，而不是拿到 3 行看着正常的空数据继续往下走。
      这正是"空值 + missing 计数"这条设计在真 DOM 上的样子。
    """
    result = await _read(site, group_index=1)
    assert not result.error, f"第 1 组本该存在：{result.error}"

    data = json.loads(result.extracted_content)
    assert data["group_tag"] == "A"
    assert data["group_members"] == 3
    assert [r["title"] for r in data["rows"]] == ["首页", "机会商品", "第二页"]
    assert data["missing"] == {"title": 0, "price": 3, "heat": 3}
    assert "price 缺 3 张" in result.long_term_memory


async def test_min_members_is_honored_on_the_real_dom(site: Site) -> None:
    """`min_members=5` → 页面上最大的组只有 4 个成员 → 明确报"一组都没有"。

    ★ 为什么值得占一次会话：它证明参数**真的进了页内脚本**，而不是被 JS 忽略掉
      （忽略掉的话这里会照常返回那 4 张卡，而"筛选没生效"在读产物时看不出来）。
      同时它把"页面上没有够大的重复块"这条 error 的措辞钉在真 DOM 上 ——
      那条措辞是 LLM 决定"换个动作还是换参数"的全部依据。
    """
    result = await _read(site, min_members=5)
    assert result.error, "min_members=5 时不该还能读到 4 张卡"
    assert "没有第 0 组重复块" in result.error
    assert "连一组够大的都没有" in result.error
