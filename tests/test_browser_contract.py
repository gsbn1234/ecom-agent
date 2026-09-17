"""真浏览器的运行时契约：把 Phase 2 的七个 spike 变成会主动失败的测试。

★ 为什么需要这个文件（而不是"spike 跑过了就行"）：
  spike 是【一次性探路】——它回答"这条路能不能走"。它跑过一次、结论写进
  docs/spikes.md 之后，就再也不会跑第二次了。而本项目有四个关键设计
  压在【未文档化的运行时行为】上（回调时序、改写生效、快照生命周期、
  私有字段存盘）。这些东西一次升级就可能变，而变了之后：
      · 代码不报错
      · 类型检查通过
      · tests/test_compat.py 可能照样绿（它只验签名在不在）
      · 只是护栏【静默失效】
  所以光有"当年跑通过"是不够的。这些行为必须每次 CI 都重新证明一遍。

★ 和 tests/test_compat.py 的分工（这是两个正交的哨兵，不是重复）：
      test_compat.py          → 静态：签名、版本号、保留字名单还在不在
      本文件                   → 运行时：那些签名【背后的行为】还在不在
  "Agent.__init__ 还接受 register_new_step_callback" 和
  "回调真的会被调用，且改写真的生效" 是两件事。
  前者是后者的必要条件，不是充分条件 —— 一个只做前者的哨兵，
  在"回调被调用但返回值被忽略"这种改动面前完全无能。

★ 与 spike 的差异（不是"搬过来"就完了）：
  spike 里有些断言是【有条件】的 —— 因为那时候还在发现阶段，不知道会看到什么。
  变成测试后一律改成【无条件断言它们当年看到的那件事】。
  这不是把测试写严了，而是把"当年验出来的结论"真的锁住：
  一个 `if 条件成立才断言` 的测试，在条件不成立时是【静默通过】的，
  读者却会以为这件事被验证过了。

★ 全部标 needs_browser：本机是硬门禁，CI 是尽力跑（见 pytest.ini 与 ci.yml）。
  理由写在 README：真实浏览器在 CI 上装不上/起不来是常态，
  与其让它把 CI 染红、逼人去看一个和代码无关的失败，
  不如明确写成"尽力跑"，然后把"本地必跑"说清楚。
"""
from __future__ import annotations

import base64
import hashlib
import sys
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _p in (TESTS_DIR, PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from browser_use.agent.views import AgentHistoryList  # noqa: E402

from ecom_agent.config import PROJECT_ROOT as CONFIG_PROJECT_ROOT  # noqa: E402
from ecom_agent.dsl.compiler import compile_task  # noqa: E402
from ecom_agent.dsl.loader import load_task  # noqa: E402
from ecom_agent.guardrails.rules import Decision  # noqa: E402
from ecom_agent.sites.pinduoduo.output_models import ProductRowList  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402
from stubs.site import (  # noqa: E402
    Site,
    index_blocks,
    local_site,
    looks_like_png,
    make_action,
    make_agent,
)

pytestmark = pytest.mark.needs_browser


@pytest.fixture
def site():
    """本地小站点。两个主机名指向同一个服务器 —— 见 stubs/site.py 的说明。"""
    with local_site() as s:
        yield s


# ── 小工具 ────────────────────────────────────────────────
def _errors(history: Any) -> list[str]:
    return [r.error for step in history.history for r in (step.result or []) if r.error]


def _action_names(step: Any) -> list[str]:
    return [
        next(iter(a.model_dump(exclude_unset=True)))
        for a in (step.model_output.action if step.model_output else [])
    ]


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ══════════════════════════════════════════════════════════
# S1 —— 护栏的首选机制：回调里改写动作
# ══════════════════════════════════════════════════════════
async def test_rewrite_actions_in_new_step_callback(site: Site) -> None:
    """R1 的运行时证据：`register_new_step_callback` 会被调用，且改写会生效。

    ★ 为什么判据是"最终 URL 落在被改写到的页面"，而不是"回调被调用了"：
      "被调用"是个安慰剂式的判据 —— 调用了但改写无效，护栏等于不存在。
      走到 /page2 有两条独立路径：LLM 的脚本 vs 我们的改写。
      脚本说的是去 /，我们改成 /page2。所以 URL 是 /page2 就只能是改写生效了。

    ★ 为什么还断言 `rewritten == 1` 和 done 能穿过：
      第一版 spike 写的是【无条件】改写，`done` 也被换成了 navigate，
      于是 run 永远结束不了、只能靠 max_steps 收场 —— 而"URL 里有 /page2"照样成立。
      一条会放过"把任务搞死"的断言等于没有断言。
      真实拦截器会犯一模一样的错："顺手全改一遍"看起来更安全，
      实际是让 agent 再也完不成任务。所以这里必须同时证明【只改了该改的】。

    ★ 失败意味着什么：R1 触发，护栏首选机制不存在，
      要切 register_should_stop_callback + 停机续跑（回退方案 A，事实 14/15 已核实）。
    """
    import asyncio

    llm = FakeLLM(
        script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"done": {"text": "结束", "success": True}},
        ]
    )
    agent = make_agent(llm, task="打开页面然后结束", allowed_domains=["127.0.0.1"])

    seen: list[int] = []
    heartbeats: list[int] = []
    rewritten = 0

    async def on_new_step(browser_state: Any, model_output: Any, step_index: int) -> None:
        nonlocal rewritten
        seen.append(step_index)
        actions = [a.model_dump(exclude_unset=True) for a in model_output.action]
        if any("navigate" in a for a in actions):
            rewritten += 1
            model_output.action = [make_action(agent, "navigate", url=site.allowed + "/page2")]

    async def heartbeat() -> None:
        """★ 独立协程，证明回调里 await 期间事件循环还在转。

        这条对应 R3：审批要放在这个回调里 await 人，而它之所以可行，
        正是因为这里的 await 不会把事件循环卡住（否则 CDP 心跳就断了）。
        真审批会 await 一个 asyncio.Event，和这里的 sleep 是同一种挂起。
        """
        while True:
            await asyncio.sleep(0.05)
            heartbeats.append(len(seen))

    hb = asyncio.create_task(heartbeat())
    agent.register_new_step_callback = on_new_step
    try:
        history = await agent.run(max_steps=4)
    finally:
        hb.cancel()
        await agent.browser_session.kill()

    urls = [u or "" for u in history.urls()]
    assert seen, "register_new_step_callback 从未被调用 —— 护栏的首选机制不存在"
    assert any("/page2" in u for u in urls), (
        f"改写 agent_output.action 没生效：URL 里没有 /page2。urls={urls}"
    )
    assert rewritten == 1, (
        f"预期只改写 1 步，实际 {rewritten} 步 —— 回调改多了。\n"
        f"  真实拦截器犯这个错时，agent 会再也完不成任务，而'URL 里有 /page2'照样成立。"
    )
    assert history.is_done() and history.final_result() == "结束", (
        f"done 没能穿过回调 —— agent 已经无法正常结束任务了。"
        f"is_done={history.is_done()}, final={history.final_result()!r}"
    )
    assert len(heartbeats) > 1, "回调执行期间事件循环停了 —— 那么在这里 await 人工审批会掐断 CDP 心跳"


# ══════════════════════════════════════════════════════════
# S2 —— 元素文本：护栏按它判，所以它必须是 LLM 看到的那个
# ══════════════════════════════════════════════════════════
async def _capture_probe(probe_site: Site, *, keep_alive: bool) -> tuple[Any, dict[int, dict[str, Any]], Any]:
    """跑一轮探针页，在回调里【当场】取走要用的东西，返回 (agent, 快照, history)。

    ★★ 必须当场取走，不能存下 browser_state 对象晚点再读。
      第一版就是这么写的，读出来 selector_map 是空的 ——
      因为那个 dict 与会话内部缓存是同一个对象，会话一 reset 就被 .clear()。
      护栏拦截器天生在回调里，所以它没这个问题；
      但"先把状态收起来、最后统一处理"的写法（Recorder 很容易这么写）会中招。
      这条坑的守卫在 test_snapshot_is_cleared_in_place。
    """
    llm = FakeLLM(
        script=[
            {"navigate": {"url": probe_site.allowed + "/probe"}},
            {"done": {"text": "结束", "success": True}},
        ]
    )
    agent = make_agent(
        llm,
        task="打开探针页然后结束",
        allowed_domains=["127.0.0.1"],
        keep_alive=keep_alive,
    )

    snapshots: dict[int, dict[str, Any]] = {}

    async def on_new_step(browser_state: Any, model_output: Any, step_index: int) -> None:
        url = browser_state.url or ""
        if not url.endswith("/probe"):
            return
        selector_map = browser_state.dom_state.selector_map
        snapshots[step_index] = {
            "url": url,
            # 与拦截器将要做的完全一致：按 index 取节点，再取可读文本
            "texts": {i: (n.get_meaningful_text_for_llm() or "") for i, n in selector_map.items()},
            "dom_text": browser_state.dom_state.llm_representation(),
            "summary": browser_state,
        }

    agent.register_new_step_callback = on_new_step
    history = await agent.run(max_steps=4)
    return agent, snapshots, history


def _visible_text(block: str) -> str:
    """把一段序列化结果里的索引、shadow 标记、标签和属性都剔掉，只留 LLM 能看见的文本内容。

    ★ 必须把属性一起剔掉，否则 `<input placeholder="删除" />` 会被算成"有可见文本"。
      它的文本是【通过属性】给 LLM 看的，那种情况不算盲区。
      盲区是 `<button>删除</button>` 这种：文本明摆在内容里，而
      get_meaningful_text_for_llm() 却返回空 —— 那护栏就永远匹配不上。
    """
    import re

    s = re.sub(r"\|SHADOW\([^)]*\)\|", "", block)
    s = re.sub(r"\[\d+\]", "", s)
    s = re.sub(r"<[^>]*>", "", s)
    return s.strip()


async def test_meaningful_text_drives_policy(site: Site) -> None:
    """护栏的命门：`get_meaningful_text_for_llm()` 取到的就是 LLM 看到的那个文本。

    ★ 为什么这是命门：Layer 1 的 `match_element_text` 规则（"删除|批量删除|下架|…"）
      是按【文本】判定的。文本取错，护栏有两种坏法，而【两种都不发出声音】：
        · 规则永不命中 → 护栏静默失效，所有测试还是绿的（没命中就不报错）
        · 命中的是 LLM 根本看不见的东西 → 误拦，LLM 反复改道、任务卡死

    ★ 判据刻意不用计划里原本写的"打出对照表肉眼一致"：
      肉眼一致没有证伪力 —— 表里二十行，人眼只会去挑自己期待的那几行看。
      换成三条可执行的断言：

      1. 取值优先级：探针页上每个元素【只填一种】文本来源，所以"谁赢了"唯一且可证伪
      2. 与 LLM 所见一致：每个节点的可读文本都出现在序列化后的 DOM 文本里
         （docstring 的原话是 "matches exactly what goes into the DOMTreeSerializer output"）
         + 反向探针：有没有"可读文本为空、但 LLM 明明看到文本"的元素
      3. 接到真策略上：批量删除 → block、立即支付 → confirm

      第 3 条才是最终价值 —— 它证明 "index → 文本 → 决策" 这条链整条是通的。
      前两条单独看都只是"文本对不对"，只有第 3 条能说明护栏真的在按它工作。

    ★ 失败意味着什么：R5 触发。退路 1 = 改用 llm_representation() 整段匹配；
      退路 2 = 只用 action 名 + URL + param_regex，并在 docs/guardrail_design.md
      里诚实标注"拦不住按文本判定的规则"。两条退路都会让护栏精度下降，
      所以这条测试红了要当回事，不是改断言了事。
    """
    agent, snapshots, history = await _capture_probe(site, keep_alive=False)
    await agent.browser_session.kill()

    assert snapshots, f"没抓到探针页的浏览器状态。urls={history.urls()}"
    snap = snapshots[sorted(snapshots)[0]]
    texts: dict[int, str] = snap["texts"]
    dom_text: str = snap["dom_text"]

    def find(sub: str) -> list[int]:
        return [i for i, t in texts.items() if sub in t]

    # ── 判据 1：取值优先级 ────────────────────────────
    assert find("仅value值"), f"value 没赢过 placeholder。实际拿到的全部文本：{sorted(texts.values())}"
    assert not find("这个占位符该输给value"), (
        "placeholder 越过了 value —— 优先级和 dom/views.py:616 的列表不一致"
    )
    assert find("仅aria标签") and not find("这个占位符不该赢"), (
        f"aria-label 没赢过 placeholder。实际：{sorted(texts.values())}"
    )
    assert find("仅title属性"), f"title 没被取到。实际：{sorted(texts.values())}"
    assert find("仅占位符"), f"placeholder 没被取到。实际：{sorted(texts.values())}"
    assert find("嵌套在span里的下架"), (
        f"嵌套子元素的文本没被取到（兜底路径失效）。实际：{sorted(texts.values())}"
    )
    # alt 只报告不断言：img 算不算"可交互元素"取决于序列化器的判定，
    # 而那不在护栏的关心范围内 —— 护栏只判点击目标有没有可读文本。
    print(f"[S2] img 的 alt 是否进入 selector_map：{bool(find('仅alt文本'))}")

    # ── 判据 2：可读文本确实出现在 LLM 读到的文本里 ────
    blocks = index_blocks(dom_text)
    assert blocks, f"序列化文本里一段带索引的都没有，格式假设不成立：\n{dom_text[:500]}"
    assert list(blocks) == sorted(blocks), (
        "index_blocks 抽出来的索引不是升序 —— 段落的切分方式可能已经不对了，"
        "后面所有基于它的断言都不可信"
    )
    mismatched = [
        (i, t, blocks.get(i, "<该索引不在序列化结果里>"))
        for i, t in texts.items()
        if t and t not in blocks.get(i, "")
    ]
    assert not mismatched, (
        "可读文本和 LLM 实际读到的对不上（docstring 那句 'matches exactly' 不成立）：\n"
        + "\n".join(f"  [{i}] 可读={t!r}  序列化段={b!r}" for i, t, b in mismatched)
    )

    # ★ 反向探针：可读文本为空、但 LLM 在序列化结果里明明看得到文本。
    #   这是最危险的一类 —— 规则永远匹配不上，而且不报任何错。
    blind = [i for i, t in texts.items() if not t and _visible_text(blocks.get(i, ""))]
    assert not blind, (
        f"这些索引的可读文本是空的，但 LLM 在序列化结果里看得到文本：{blind}\n"
        + "\n".join(f"  [{i}] {blocks.get(i)!r}" for i in blind)
    )

    # ── 判据 3：接到真策略上 ──────────────────────────
    # ★ 用 tasks/pdd_search_products.yaml 编译出的【真 policy】，不是测试里现搓的规则集。
    #   现搓的话，这条测试就只能证明"policy.evaluate 是台能跑的机器"，
    #   证明不了"我们真正要用的那份规则集能拦住真正危险的那些按钮"。
    spec = load_task(PROJECT_ROOT / "tasks" / "pdd_search_products.yaml")
    compiled = compile_task(spec, {"keyword": "保温杯"})
    policy = compiled.policy
    url = snap["url"]

    def judge(sub: str) -> Any:
        idx = find(sub)
        assert idx, f"探针页上的『{sub}』没进 selector_map，判据 3 无法成立"
        return policy.evaluate("click", {"index": idx[0]}, url, texts[idx[0]])

    verdicts = {}
    for probe in ("批量删除", "立即支付", "编辑", "搜索"):
        if find(probe):
            verdicts[probe] = judge(probe)
    print("[S2] 真策略判定：" + "；".join(
        f"{k}={v.decision.value}(rule={v.rule_id or '<default>'})" for k, v in verdicts.items()
    ))

    assert verdicts["批量删除"].is_blocked, (
        f"真策略没拦住『批量删除』：{verdicts['批量删除'].decision.value} "
        f"rule={verdicts['批量删除'].rule_id}"
    )
    assert verdicts["立即支付"].decision is Decision.CONFIRM, (
        f"『立即支付』应为 confirm，实际 {verdicts['立即支付'].decision.value}"
    )
    assert not verdicts["搜索"].is_blocked, (
        f"只读的『搜索』被拦了：rule={verdicts['搜索'].rule_id} —— "
        f"护栏过严会把正常任务卡死，和过松一样是缺陷"
    )

    # ★★ 这一条不是"顺手多写一条"，它锁住的是 ADR-7（default_decision=confirm），
    #    而且是【在真实页面上、走完整条链】锁住的：
    #    index → 真实 DOM 的元素文本 → 策略判定。
    #    离线单测只能证明"policy.evaluate 在给定文本下会返回 confirm"，
    #    证明不了"真实页面上取到的文本会让它走到默认分支"。
    #
    #    为什么『搜索』落到默认分支：真策略里唯一能放行只读操作的 allow-readonly
    #    规则带了 `match_url: "*mms.pinduoduo.com/*"` 的作用域，而这里的主机是
    #    127.0.0.1 —— 不在作用域内，所以没有任何规则命中，于是由 default_decision
    #    说了算，而它是 confirm 而不是 allow。这正是 fail-closed 在起作用：
    #    护栏只在"确认过安全"的地方放行，不在"没写过规则"的地方放行。
    #
    #    ⚠️ Phase 4 的直接后果：mock 后台的 e2e 如果不带自己的 URL 作用域，
    #    每一次"点搜索"都要人工批一次，e2e 会变成人肉点击流水线。
    #    所以 mock 的任务模板必须自带 `match_url` —— 这条断言就是那个需求的
    #    证据，而不是一句"我记得好像会这样"。
    #    （这里原先写的是"platform/mock.yaml 必须有自己的 match_url"，但那个
    #      文件始终没建：作用域落在了 tasks/mock_shop_readonly.yaml 的
    #      allow-readonly 规则上。**指向不存在的文件 = 下一处漂移**。）
    assert verdicts["搜索"].decision is Decision.CONFIRM, (
        f"『搜索』的判定是 {verdicts['搜索'].decision.value}，预期 confirm（default_decision）。\n"
        f"  · 若变成 allow → 说明有一条作用域外的规则放行了它，fail-closed 被破坏，ADR-7 要重审\n"
        f"  · 若变成 block → 护栏过严，正常只读任务会被卡死\n"
        f"  两种情况都意味着 mock 任务模板的 URL 作用域（tasks/mock_shop_readonly.yaml"
        f" 的 match_url）这个设计前提变了。"
    )


async def test_snapshot_is_cleared_in_place(site: Site) -> None:
    """持有的 `BrowserStateSummary` 会被会话 reset【原地清空】—— 外加对照实验。

    ★ 这条比文本那几条更危险，因为它的失败形态是【静默的】：
      一个存了 browser_state、想在最后统一处理的地方（Recorder 很容易这么写），
      会拿到一个空 selector_map，于是"没有任何规则命中" ——
      而没有任何规则命中是一个【合法结果】：不报错、不告警、测试还是绿的。
      查起来会指向"LLM 这一步没产生危险动作"，而真相是我们自己把证据丢了。

    ★ 为什么必须配 4b 这个对照：
      只看判据 4 会得出"只要不主动 kill 就没事"。而我们的 CompiledTask
      默认 keep_alive=True（为了停机续跑），所以"哪种配置安全"必须实测，不能推。
      4b 证明【默认配置（keep_alive=False）下，run() 自己就已经 reset 过了】——
      也就是说，这个问题不是"你多手 kill 了"，而是"默认就会发生"。

    ★ 失败意味着什么：如果 after != 0，说明库改掉了就地清空的行为，
      那么"必须当场取值"这条约束可以放松 —— 但要先去 docs/spikes.md 和 ADR
      把机制解释（session.py:664 的 .clear() + session.py:2494 的直接赋值）改掉，
      不能只改这里。
    """
    # ── 判据 4：keep_alive=True，才能看到 10 → 0 这个过程 ──
    agent, snapshots, _ = await _capture_probe(site, keep_alive=True)
    assert snapshots, "没抓到探针页的状态"
    snap = snapshots[sorted(snapshots)[0]]
    texts = snap["texts"]
    summary = snap["summary"]

    before_map = summary.dom_state.selector_map
    before = len(before_map)
    aliased = agent.browser_session._cached_selector_map is before_map
    await agent.browser_session.kill()
    after = len(summary.dom_state.selector_map)

    assert before == len(texts) > 0, (
        f"读到了 {len(texts)} 条文本，而 selector_map 只有 {before} 个 —— 计数不自洽"
    )
    assert aliased, (
        "快照的 selector_map 与会话内部缓存 _cached_selector_map 不是同一个 dict —— "
        "那么机制解释（reset 就地 clear）不成立，docs/spikes.md 要改"
    )
    assert after == 0, (
        f"预期 kill 之后被清空，实际还有 {after} 个 —— "
        f"库可能改成拷贝了，'必须当场取值'这条约束要重新评估"
    )

    # ── 判据 4b（对照）：keep_alive=False 时 run() 自己就 reset 了 ──
    agent2, snapshots2, _ = await _capture_probe(site, keep_alive=False)
    snap2 = snapshots2[sorted(snapshots2)[0]]
    at_return = len(snap2["summary"].dom_state.selector_map)
    await agent2.browser_session.kill()

    assert len(snap2["texts"]) == len(texts), "两轮抓到的状态不可比，4b 的对照不成立"
    assert at_return == 0, (
        f"预期 run() 返回时快照已被自己 reset 清空，实际还有 {at_return} 个。\n"
        f"  若如此，判据 4 关于【默认配置】的结论不成立，docs 要改。"
    )


# ══════════════════════════════════════════════════════════
# S3 —— Layer 0 挡住之后，页面变成什么
# ══════════════════════════════════════════════════════════
async def test_blocked_navigation_leaves_blank_page(site: Site) -> None:
    """被白名单拦下的导航会把标签丢在空白页 —— 所以 Layer 0 只能当"最后一道网"。

    ★ 为什么这条是设计依据而不是冷知识：
      拦下一次导航 = 顺手弄丢当前页面 = 后面的提取全采到空。
      一个"只读采集"任务跑到一半被拦一次，之后所有步骤都在 about:blank 上工作，
      而 run 本身【成功结束】、报告里只有几条空数据。
      所以 Layer 1 必须在动作执行【前】拦 —— 那样根本不产生导航，页面状态不被破坏。
      而 Layer 0 的定位只能是"最后一道网"：它挡得住越界，但挡不住副作用。

    ★ 计划里原本的判据是"站外 navigate → ActionResult 含 Navigation failed，run 不崩"。
      跑 S7 时发现那不够 —— 它没回答"拦下来之后还能不能继续干活"。
      这里补上：错误信息里能读出是【安全策略】拦的（不是网络失败），
      且被拦之后页面【不再回到原来的页面】。

    ★ 与 spike 的差异：spike 里 `if after == "about:blank"` 才打印结论，是【有条件】的
      —— 因为那时还在发现阶段。测试里改成无条件断言它当年看到的那件事。
      有条件的断言在条件不成立时静默通过，读者却以为验过了。
    """
    llm = FakeLLM(
        script=[
            {"navigate": {"url": site.allowed + "/"}},        # 进站
            {"navigate": {"url": site.forbidden + "/page2"}},  # 出站（同服务器，只换主机名）
            {"extract": {"query": "页面上有什么"}},             # 被拦之后还能不能干活
            {"done": {"text": "结束", "success": True}},
        ]
    )
    agent = make_agent(llm, task="进站、尝试出站、然后提取", allowed_domains=["127.0.0.1"])
    history = await agent.run(max_steps=6)
    await agent.browser_session.kill()

    urls = [u or "" for u in history.urls()]
    errors = _errors(history)

    # 判据 1：被拦，且原因可读 —— 必须是"安全策略"，不是"网络失败"。
    # ★ 这条区分很重要：如果是网络失败，那 Layer 0 压根没在工作，
    #   而"目标站打不开"和"护栏拦住了"在结果上长得一模一样。
    blocked = [e for e in errors if "blocked by security policy" in e]
    assert blocked, (
        f"站外导航没被拦（或错误信息换了说法）—— 那就分不清是护栏生效还是网络不通。\n"
        f"  urls={urls}\n  errors={errors}"
    )
    assert site.forbidden not in urls, f"站外地址居然进了历史。urls={urls}"

    # 判据 2：run 没崩，走到了最后 —— 护栏不该让任务崩掉
    assert history.is_done(), "被拦之后 run 中断了 —— 护栏不该让任务崩掉"
    assert history.final_result() == "结束", f"没走到 done：{history.final_result()!r}"

    # 判据 3：被拦之后页面在哪
    assert urls[-1] == "about:blank", (
        f"被拦之后页面停在了 {urls[-1]!r}，而不是空白页。\n"
        f"  urls={urls}\n"
        f"  → 若它回到了原来的页面，那么 Layer 0 可以当'拦下来之后继续干活'的机制，\n"
        f"    docs/guardrail_design.md 里'Layer 0 只能当最后一道网'这条要改。"
    )


# ══════════════════════════════════════════════════════════
# S7 —— 白名单能匹配带端口的 IP 主机名吗
# ══════════════════════════════════════════════════════════
async def test_allowlist_matches_ip_with_port_but_not_localhost(site: Site) -> None:
    """`allowed_domains=["127.0.0.1"]` 能匹配 `http://127.0.0.1:PORT`，且不匹配 localhost。

    ★ 为什么必须先验这条：Phase 4 的 mock 卖家后台就跑在 127.0.0.1:随机端口上。
      如果白名单匹配不了带端口的 IP，整个 e2e 会卡在"连站点都进不去"，
      而报错会指向"导航失败"，看起来像浏览器或网络问题 —— 排查方向全错。

    ★ 为什么必须有对照组：光断言"能打开 127.0.0.1 的页面"是不够的 ——
      白名单要是压根没生效，这条断言照样通过。
      所以同一次运行里还要断言【同一台服务器、同一端口、只换个主机名 localhost 就打不开】。
      两条断言同时成立，才能说白名单真的在按域名判定；
      而 S3 的结论（护栏确实在拦）也依赖这一点。

    ★ 失败意味着什么：mock 站点方案要改绑主机名（localhost + "localhost" 白名单项），
      或者给 SecurityWatchdog 补一条 IP:port 的匹配规则。
    """
    llm = FakeLLM(
        script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"navigate": {"url": site.forbidden + "/"}},  # 同一台服务器，只换主机名
            {"done": {"text": "结束", "success": True}},
        ]
    )
    agent = make_agent(
        llm,
        task="依次打开两个地址",
        allowed_domains=["127.0.0.1"],  # 只允许 IP，不允许 localhost
    )
    history = await agent.run(max_steps=5)
    await agent.browser_session.kill()

    urls = [u or "" for u in history.urls()]
    assert any("127.0.0.1" in u for u in urls), (
        f"白名单里的 127.0.0.1 打不开 —— mock 站点方案不成立。urls={urls}\n"
        f"  errors={_errors(history)}"
    )
    # ★ 对照：同一个服务器，只是主机名不同，必须进不去
    assert not any("localhost" in u for u in urls), (
        f"localhost 也进去了 —— 白名单没在按域名判定，S3 的结论会不可信。urls={urls}"
    )


# ══════════════════════════════════════════════════════════
# S5 —— 截图：两条路都要给真 PNG
# ══════════════════════════════════════════════════════════
async def test_screenshots_are_real_png_on_both_paths(site: Site) -> None:
    """截图的两种取法都给真 PNG，且两路拿到的是同一批帧；`use_vision=False` 下零图片进提示词。

    ★ 为什么判"是不是真 PNG"而不是"有没有截图"：
      "有没有"极易满足 —— 一段报错文本、一个被转坏的 base64、一个 0 字节文件，
      都能让 `screenshot is not None` 成立。而这些图是亮点 4（可观测回放）
      的【最终交付物】，图坏掉必须在这一层发现；
      等打开 report.html 才看到坏图，已经不知道是哪一步、哪条路径坏的了。

    ★ 两条路各自服务谁（这是设计由来，不是随便选的）：
      · new_step_callback 的 browser_state.screenshot（裸 base64，事实 12）
        —— 回调里就在手边，不碰磁盘。护栏/记录器当场判断"画面变没变"时用它最省。
      · on_step_end 的 history[-1].state.screenshot_path
        —— 库已经把图落盘了，给个路径。长期留档用它少一次编码往返。
      两路必须【给出同一批帧】：记录器用 A 存内存、报告用 B 写路径，
      不一致的话报告和库里就是两帧不同的画面，而且没人会发现。

    ★ 为什么单独验"零图片进提示词"（ADR-2 的可执行证据）：
      "截图被采集了"（前几条已证）与"截图被发给了模型"是两件事。
      合成一条的话，将来有人把 use_vision 打开，截图那条断言照样绿，
      而成本已经翻倍了（每步一张图进提示词）。

    ★ 判据 5 决定一条设计：库落的图在【系统临时目录】（关机即失），
      所以审计必须自己另存一份。这条如果只在文档里写"要另存"，没人会当真。
    """
    llm = FakeLLM(
        script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"scroll": {"down": True, "pages": 1.0}},
            {"done": {"text": "结束", "success": True}},
        ]
    )
    agent = make_agent(
        llm,
        task="打开页面、滚动一下、结束",
        agent_kw={"use_vision": False},  # ADR-2：零 VLM，纯文本 DOM 树驱动
        allowed_domains=["127.0.0.1"],
        # ★ keep_alive=True：让库的临时目录活到断言之后，好去磁盘上核那几张图。
        #   默认配置下 run() 一返回就把浏览器和中间产物都收了（事实 15），
        #   那时再去读路径只会得到"文件不存在"。
        keep_alive=True,
    )

    from_callback: dict[int, str | None] = {}
    from_hook: list[tuple[int, str | None, bool]] = []

    async def on_new_step(browser_state: Any, model_output: Any, step_index: int) -> None:
        from_callback[step_index] = browser_state.screenshot

    async def on_step_end(agent_: Any) -> None:
        h = agent_.history.history
        p = h[-1].state.screenshot_path if h else None
        from_hook.append((len(h), p, bool(p and Path(p).exists())))

    agent.register_new_step_callback = on_new_step
    history = await agent.run(max_steps=4, on_step_end=on_step_end)

    # ── 判据 1：回调那条路 ────────────────────────────
    cb_sha: dict[int, str] = {}
    for step in sorted(from_callback):
        b64 = from_callback[step]
        if not b64:
            continue
        raw = base64.b64decode(b64)
        cb_sha[step] = _sha(raw)
        assert looks_like_png(raw), (
            f"step {step} 的回调截图不是 PNG（前 8 字节 {raw[:8]!r}）—— "
            f"base64 解出来是别的东西。"
        )
    assert cb_sha, "回调里一步截图都没拿到 —— 可观测性的主路径不成立"

    # ── 判据 2：钩子那条路 ────────────────────────────
    hook_sha: list[str] = []
    for _, p, exists in from_hook:
        if not (p and exists):
            continue
        raw = Path(p).read_bytes()
        assert looks_like_png(raw), f"钩子给出的路径里，文件不是 PNG：{p}"
        hook_sha.append(_sha(raw))
    assert hook_sha, "钩子里一条可用的截图路径都没有 —— 报告那条路不成立"

    # ── 判据 3：两条路拿到的图必须是同一批 ────────────
    # ★ 不做这个对照，两条路各修各的 bug：回调那条一直好好的，
    #   钩子那条可能因为 off-by-one 落后一步。两边单测都绿，而报告里的图是错帧的。
    assert sorted(cb_sha.values()) == sorted(hook_sha), (
        "两条路给出的图不是同一批 —— 记录器用 A、报告用 B 的话，两边会是不同的帧。\n"
        f"  A={sorted(v[:12] for v in cb_sha.values())}\n"
        f"  B={sorted(v[:12] for v in hook_sha)}"
    )

    # ── 判据 4：ADR-2 的可执行证据 ────────────────────
    img_parts = llm.total_image_parts()
    assert img_parts == 0, (
        f"use_vision=False 下仍有 {img_parts} 个图片分片进了提示词 —— "
        f"要么配置没生效，要么库改了行为，ADR-2 的成本论证要重算"
    )

    # ── 判据 5：库落的图在系统临时目录（决定"必须自己另存"）──
    lib_paths = [p for _, p, ok in from_hook if ok and p]
    tmp = Path(tempfile.gettempdir())
    assert all(Path(p).is_relative_to(tmp) for p in lib_paths), (
        "库落的截图不在系统临时目录下 —— 那么'关机即失、审计必须自己另存'"
        f"这个设计前提要重新核实。实际路径：{[str(p) for p in lib_paths]}"
    )
    assert not any(Path(p).is_relative_to(CONFIG_PROJECT_ROOT) for p in lib_paths), (
        f"截图落在了项目目录下（{lib_paths}）—— 那 .gitignore 挡 runs/ 就不够了，"
        f"得把库的 agent_directory 也挡上，否则截图会进 git"
    )

    # ── 判据 6：库自己的访问器给的是同一批路径 ────────
    via_history = [p for p in history.screenshot_paths() if p]
    assert sorted(via_history) == sorted(lib_paths), (
        f"history.screenshot_paths() 和钩子里看到的对不上：{via_history} vs {lib_paths}"
    )

    # ── 判据 7：sha 相同 = "画面没变"，但它是【原始信号】，不是结论 ──
    # ★ 跑 S5 时的意外收获。计划里写的是"连续两步 sha 相同 →
    #   报告里标注『页面未变化（可能是点击无效）』"。实测发现这个标注下早了：
    #
    #   本例里各步 sha 完全相同，而其中一步的动作是 scroll ——
    #   日志明写 "Scrolled down 1080px"，它【成功执行了】，
    #   只是这个 mock 页面比视口还短，滚不动，画面自然没变。
    #   于是"画面没变"在这里是【完全正常】的，而同一句话用在 click 上就是可疑的。
    #
    #   结论：sha 相同只是"画面没变"这个原始事实，要能和动作名一起看才有意义。
    #   报告里必须把动作名和这个标注【并排】显示，否则读者会把正常行为当故障查。
    #
    # ⚠️ 这一条是【有条件】的，和前面几条不同 —— 因为"所有步画面相同"取决于
    #   mock 页面比视口短这个物理事实，不是库的行为。
    #   条件不成立时它不校验，但下面那行 print 总会打出对照表，
    #   所以"报告要不要并排显示动作名"这个问题在两种情况下都有材料可看。
    acts = [(i + 1, _action_names(h), [r.error for r in (h.result or []) if r.error])
            for i, h in enumerate(history.history)]
    print("[S5] 动作/截图对照：" + "；".join(
        f"step{s}: {'+'.join(n) or '<无动作>'} sha={(cb_sha.get(s) or '<无>')[:12]} err={e}"
        for s, n, e in acts
    ))

    if len(set(cb_sha.values())) == 1:
        assert len(cb_sha) > 1, "只有一步有截图，'连续两步相同'无从谈起"
        assert any("scroll" in n for _, n, _ in acts), (
            f"预期本例的相同画面来自 scroll 步，实际动作序列是 {[n for _, n, _ in acts]}"
        )
        assert not any(e for _, _, e in acts), (
            f"有步骤报错了 —— 那'画面没变'就不是正常行为，本判据的结论不成立：{acts}"
        )

    await agent.browser_session.kill()


# ══════════════════════════════════════════════════════════
# S6 —— 结构化输出：存盘再读回还拿不拿得到
# ══════════════════════════════════════════════════════════
_ROWS = {
    "rows": [
        {"goods_id": "100001", "title": "保温杯 316不锈钢", "price": "59.90",
         "stock": 120, "status": "在售中"},
        {"goods_id": "100002", "title": "保温杯 便携款", "price": "¥39.00",
         "stock": 45, "status": "在售中"},
    ],
    "keyword": "保温杯",
    "note": "",
}


async def test_structured_output_survives_roundtrip_via_getter(site: Site) -> None:
    """落盘再读回后 `.structured_output` 变 None，但 `get_structured_output(Model)` 仍然可用。

    ★ 为什么这条是"结构化落库"（亮点 2）的地基：
      我们的产物是 runs/{id}/result.json + sqlite 里的行，所以【一定会】发生
      "跑完 → 序列化落盘 → 之后某个时刻读回来用"。如果那条路上拿不到模型：
        · 报告里"采集到的商品"是空的
        · 入库的行数是 0
        · 而 run 本身是成功的，没有任何报错
      一个"成功但什么都没采到"的 run，比一个失败的 run 难查得多 ——
      失败的 run 会喊，成功的空 run 不会。

    ★ 为什么第一条断言是【对照组】：
      不先证明"落盘之前是好的"，那么"落盘后是 None"就可能只是
      "结构化输出压根没工作"（两边都是 None，断言恒成立）。
      对照实验是这个项目的招牌手法：每条"否定"断言旁边都要有一条"肯定"断言。

    ★ 判据 5 解释机制：`_output_model_schema` 是 pydantic 私有属性，不进 model_dump，
      所以读回来的对象上它是默认值 None。property 于是【静默】返回 None ——
      它长得像"这次没采到数据"，而不像"你调错方法了"。
      这就是为什么落库/报告一律走 getter（事实 11）。

    ★ 判据 4b 影响一条设计：`final_result()` 是【清洗后】的形态，
      不是 LLM 的原话 —— 它是 model_dump(mode='json') 的产物（tools/service.py:2017）。
      所以"把 final_result_raw 留着，以后能看 LLM 当时到底返回了什么"这个想法不成立。
      要留 LLM 原话，只能自己在 steps.jsonl 里记。
      把两者混为一谈，排查时会对着一份"看起来正常"的数据想不通为什么会被隔离。
    """
    import json

    llm = FakeLLM(
        script=[
            {"navigate": {"url": site.allowed + "/"}},
            # ★ 有 output_model_schema 时，done 的参数模型被换成
            #   StructuredOutputAction[ProductRowList]（tools/service.py:2010-2014），
            #   所以脚本里写 data=... 而不是 text=...。
            #   这本身就是一条事实：加了 output_model_schema 之后，
            #   LLM 的"结束"动作的 schema 变了 —— 提示词里那份 JSON schema 是它唯一的说明。
            {"done": {"data": _ROWS, "success": True}},
        ]
    )
    agent = make_agent(
        llm,
        task="打开页面并返回结构化结果",
        agent_kw={"output_model_schema": ProductRowList},
        allowed_domains=["127.0.0.1"],
        keep_alive=True,
    )
    history = await agent.run(max_steps=4)

    # ── 判据 1（对照）：落盘【之前】，property 是好的 ──
    before = history.structured_output
    assert before is not None, (
        "刚落盘时 structured_output 就是 None —— 结构化输出压根没工作，"
        "后面'落盘后变 None'的断言就成了空转（两边都是 None）"
    )
    assert isinstance(before, ProductRowList), f"拿到的不是 ProductRowList：{type(before)}"
    assert [r.goods_id for r in before.rows] == ["100001", "100002"], (
        f"行内容不对：{[r.goods_id for r in before.rows]}"
    )
    # ★ price 故意写成两种形态（裸数字串 / 带 ¥ 前缀），走 output_models.clean_price
    #   那条确定性清洗。清洗没生效的话 Decimal 校验会直接报错，
    #   所以"能解析出来"本身就证明清洗链是通的，不需要另外断言。
    assert before.rows[1].price == Decimal("39.00"), (
        f"'¥39.00' 没被清洗成 Decimal('39.00')，实际 {before.rows[1].price!r}"
    )
    assert before.keyword == "保温杯"

    # ── 落盘 → 读回 ───────────────────────────────────
    dumped = history.model_dump_json()
    restored = AgentHistoryList.model_validate_json(dumped)

    # ── 判据 5：私有字段根本没被写出去 ────────────────
    assert "_output_model_schema" not in dumped, (
        "私有字段居然进了序列化产物 —— 那判据 2 的机制解释（'从来没写出去'）不成立"
    )

    # ── 判据 2：读回之后 property 变成 None，而且是静默的 ──
    assert restored.structured_output is None, (
        f"读回之后 property 竟然还能用（{restored.structured_output!r}）—— "
        f"库修好了这件事，docs 和 ADR-9 要改，我们也不再需要绕私有字段"
    )

    # ── 判据 3：getter 在同一个读回对象上仍然可用 ─────
    after = restored.get_structured_output(ProductRowList)
    assert after is not None, (
        "get_structured_output 也拿不到 —— 两条路都断，必须自己存原始 JSON 再 model_validate_json"
    )
    assert after == before, (
        "读回来的模型和原来的不相等 —— 那么落盘这一步是有损的，比'拿不到'更麻烦"
    )

    # ── 判据 4：兜底路径 —— 原始 JSON 字符串本身 ──────
    raw = history.final_result()
    assert restored.final_result() == raw, "final_result() 没挺过序列化往返"
    assert ProductRowList.model_validate_json(raw) == before, (
        "兜底路径（自己存原始 JSON）解析出来的和原来的不等价"
    )

    # ── 判据 4b：这份"原始字符串"其实【已经被清洗过了】──
    assert json.loads(raw) == before.model_dump(mode="json"), (
        f"final_result() 和模型的 JSON 形态不一致：\n  raw={raw[:200]}\n"
        f"  model={json.dumps(before.model_dump(mode='json'), ensure_ascii=False)[:200]}"
    )
    assert "¥" not in raw, (
        "final_result() 里居然保留了 '¥' 前缀 —— 那它就是 LLM 的原话，"
        "本判据的结论（它是清洗后的形态）不成立"
    )
    assert '"39.00"' in raw, f"没找到清洗后的价格，raw={raw[:200]}"

    await agent.browser_session.kill()


# ══════════════════════════════════════════════════════════
# S4 —— 一次 run 到底发了几次 LLM 请求
# ══════════════════════════════════════════════════════════
async def test_run_costs_more_llm_calls_than_steps(site: Site) -> None:
    """一次 run 的 LLM 调用次数 = 步数 + judge + extract，且 `use_vision=False` 下零图片。

    ★ 为什么这对"搭桩"和"省钱"都重要：
      我们一直默认"一个 run 有几个 step 就是几次 LLM 调用"。实测不是 ——
      库在 run 末尾会额外发一次 judge 调用（use_judge 默认开），
      而 extract 动作内部还会再发一次（它不是读页面，是问模型）。
      对一个按 token 计费的项目，"我以为 3 次、实际 5 次"是必须在架构阶段就知道的事。
      → 所以采集走结构化 `done` 而不是 `extract`，不只是"更结构化"，也是"更省"。

    ★ 为什么分组统计而不是只数个数：
      知道"多了 2 次"不够，得知道多的是【什么】——
      judge 是每次 run 固定一次，extract 是按动作次数可重复的，
      两者的增长方式完全不同（一个 O(1)，一个 O(步数)）。
      预算模型要靠这个区别才能写对。

    ★ 同时锁住 FakeLLM 是纯鸭子类型（MRO 里除 object 无别的基类）：
      桩一旦继承库的内部基类，库改基类就会让桩失效 ——
      而那正是这个桩被设计成鸭子类型要避免的事（事实 18）。
      这条断言的意义在于"我没继承"这件事会被人无意改掉。

    ★ 失败意味着什么：judge 或 extract 不再发请求，成本模型要重算；
      或者有人给 FakeLLM 加了基类，桩和库耦合上了。
    """

    def _kind(output_format: object) -> str:
        # ⚠️ 别写 `type(output_format).__name__`：output_format 是个【类】，
        #   而 `type(类)` 是它的元类 —— judge 会被归成 "ModelMetaclass"。
        #   统计代码本身出错时不会报错，只会安静地把东西归到一栏你从没见过的名字下面。
        if output_format is None:
            return "<抽取类: output_format=None>"
        return getattr(output_format, "__name__", type(output_format).__name__)

    bases = [b.__name__ for b in FakeLLM.__mro__[1:]]
    assert bases == ["object"], (
        f"FakeLLM 多了基类 {bases} —— 桩一旦继承库的内部基类，"
        f"库改基类就会让桩失效，而那正是这个桩被设计成鸭子类型要避免的事"
    )

    llm = FakeLLM(
        script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"extract": {"query": "页面上有什么"}},
            {"done": {"text": "结束", "success": True}},
        ]
    )
    agent = make_agent(
        llm,
        task="打开页面、抽取一次、然后结束",
        agent_kw={"use_vision": False},
        allowed_domains=["127.0.0.1"],
        keep_alive=True,
    )
    history = await agent.run(max_steps=6)

    urls = [u or "" for u in history.urls()]
    assert history.is_done(), "run 没走到 done"
    assert history.final_result() == "结束", f"final_result={history.final_result()!r}"
    assert any(site.allowed in u for u in urls), (
        f"脚本里的 navigate 没生效 —— URL 历史里没有站点地址。urls={urls}"
    )
    assert not llm.exhausted, (
        f"脚本被跑穿了（步数比脚本长，实际 {llm.steps} 步）—— 说明有一步没按预期消耗脚本"
    )

    kinds = Counter(_kind(c.kwargs.get("output_format")) for c in llm.other_calls)
    assert kinds.get("JudgementResult", 0) >= 1, (
        f"没看到 run 末尾的 judge 调用（use_judge 默认 True）—— "
        f"要么库改了默认值，要么它走了别的 llm 实例。实际 kinds={dict(kinds)}"
    )
    assert kinds.get("<抽取类: output_format=None>", 0) >= 1, (
        f"extract 动作没有引发额外的 LLM 调用 —— 那么'抽取不是免费的'这条结论不成立。"
        f"实际 kinds={dict(kinds)}"
    )
    assert llm.total_image_parts() == 0, (
        f"use_vision=False 下仍有 {llm.total_image_parts()} 个图片分片进了提示词"
    )

    await agent.browser_session.kill()
