"""S2 —— 护栏的「元素文本」从哪来：get_meaningful_text_for_llm() 到底准不准。

★ 为什么这是护栏的命门：
  Layer 1 的 match_element_text 规则（"删除|批量删除|下架|..."）是按【文本】判定的。
  拿不到点击目标的可读文本，或者拿到的跟 LLM 看到的不是一回事，护栏就两种坏法：
    · 规则永不命中 → 护栏静默失效，而所有测试都是绿的（没规则命中就不会报错）
    · 命中的是 LLM 根本看不见的东西 → 误拦，LLM 反复改道、任务卡死
  两种坏法都不会自己发出声音。

★ 判据刻意【不用】计划里原本写的"打出对照表肉眼一致"。
  肉眼一致没有证伪力：表里二十行，人眼只会去挑自己期待的那几行看。
  换成四条可执行的断言：

    1. 【取值优先级】每个探针元素的文本确实来自我们指定的那个属性
       —— 探针页上每个元素只填一种文本来源，冒出别的文本就说明优先级和以为的不同
    2. 【与 LLM 所见一致】每个节点的可读文本都出现在序列化后的 DOM 文本里
       —— docstring 的原话是 "matches exactly what goes into the DOMTreeSerializer output"
    3. 【接到真策略上】用 tasks/pdd_search_products.yaml 编译出的【真 policy】
       去判这一页的【真实元素】：批量删除 → block、立即支付 → confirm
    4. 【快照不是不可变的】持有的 BrowserStateSummary 在会话 reset 后 selector_map 会变空

  第 3 条才是最终价值：它证明 "index → 文本 → 决策" 这条链整条是通的。
  第 4 条是这个 spike 自己踩出来的坑（见下），它决定了拦截器【必须怎么写】。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_lib import local_site, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

from ecom_agent.dsl.compiler import compile_task  # noqa: E402
from ecom_agent.dsl.loader import load_task  # noqa: E402
from ecom_agent.guardrails.rules import Decision  # noqa: E402

TAG = "S2"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _index_blocks(dom_text: str) -> dict[int, str]:
    """从序列化文本里抽出 {元素索引: 该元素在 LLM 眼里对应的那一段}。

    ★★ 一次踩出来的教训：元素的那一段【不只是一行】。
      实测序列化结果是长这样的：

          [25]<button />
          	搜索

      子元素的文本被放到【下一行的缩进】里，而不是塞在标签中间。
      第一版我按"一个索引 = 一行"去匹配，于是每个按钮都判成"文本对不上" ——
      而那是我自己的解析错了，不是库的行为错了。
      假警报和漏报一样费时间，而且更打击人：它会让你去改一个本来正确的东西。

    ★ 为什么用正则扫 LLM 实际读到的那个字符串，而不是走库的内部结构：
      判据必须建立在"发给 LLM 的那段文本"上。走内部结构取到的可能是
      "生成该字符串的中间态"，那样即使两者不一致，断言也会通过 ——
      而两者不一致恰恰是我们要发现的东西。
    """
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


def _visible_text(block: str) -> str:
    """把一段序列化结果里的索引、shadow 标记、标签和属性都剔掉，只留 LLM 能看见的文本内容。

    ★ 必须把属性一起剔掉，否则 `<input placeholder="删除" />` 会被算成"有可见文本"。
      它的文本是【通过属性】给 LLM 看的，那种情况不算盲区。
      盲区是 `<button>删除</button>` 这种：文本明摆在内容里，而
      get_meaningful_text_for_llm() 却返回空 —— 那护栏就永远匹配不上。
    """
    s = re.sub(r"\|SHADOW\([^)]*\)\|", "", block)
    s = re.sub(r"\[\d+\]", "", s)
    s = re.sub(r"<[^>]*>", "", s)
    return s.strip()


async def _capture(site: Any, *, keep_alive: bool) -> tuple[Any, dict[int, dict[str, Any]], Any]:
    """跑一轮，在回调里【当场】取走要用的东西，返回 (agent, 快照, history)。"""
    llm = FakeLLM(script=[
        {"navigate": {"url": site.allowed + "/s2"}},
        {"done": {"text": "结束", "success": True}},
    ])
    agent = make_agent(
        llm,
        task="打开探针页然后结束",
        allowed_domains=["127.0.0.1"],
        keep_alive=keep_alive,
    )

    # ★★ 状态在 new_step_callback 里【当场】取走要用的东西，而不是存下对象晚点再读。
    #    第一版我存的是 browser_state 对象本身，跑完再读 —— 读出来 selector_map 是空的。
    #    原因见判据 4：那个 dict 与会话内部缓存是同一个对象，会话一 reset 就被 .clear()。
    #    护栏拦截器天生就在这个回调里，所以它天然没有这个问题；
    #    但"先把状态收起来、最后统一处理"的写法（Recorder 很容易这么写）会中招。
    snapshots: dict[int, dict[str, Any]] = {}

    async def on_new_step(browser_state: Any, model_output: Any, step_index: int) -> None:
        url = browser_state.url or ""
        if not url.endswith("/s2"):
            return
        selector_map = browser_state.dom_state.selector_map
        snapshots[step_index] = {
            "url": url,
            # 与拦截器将要做的完全一致：按 index 取节点，再取可读文本
            "texts": {i: (n.get_meaningful_text_for_llm() or "") for i, n in selector_map.items()},
            "dom_text": browser_state.dom_state.llm_representation(),
            # 留一份原始引用专门用来验判据 4
            "summary": browser_state,
        }

    agent.register_new_step_callback = on_new_step
    history = await agent.run(max_steps=4)
    return agent, snapshots, history


async def body() -> None:
    with local_site() as site:
        agent, snapshots, history = await _capture(site, keep_alive=True)

        assert snapshots, f"没抓到 /s2 的浏览器状态。urls={history.urls()}"
        snap = snapshots[sorted(snapshots)[0]]
        texts: dict[int, str] = snap["texts"]
        dom_text: str = snap["dom_text"]

        print("── index → 可读文本 ──")
        for i in sorted(texts):
            print(f"  [{i:>2}] {texts[i]!r}")

        print("\n── 序列化后的 DOM（只打印带索引的段）──")
        for i in sorted(_index_blocks(dom_text)):
            print("  ", _index_blocks(dom_text)[i].replace("\n", " ⏎ "))

        def find(sub: str) -> list[int]:
            return [i for i, t in texts.items() if sub in t]

        # ── 判据 1：取值优先级 ────────────────────────────
        # 每个元素只填了一种来源，所以"谁赢了"是唯一的、可证伪的。
        assert find("仅value值"), (
            f"value 没赢过 placeholder。实际拿到的全部文本：{sorted(texts.values())}"
        )
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

        # alt 只报告不断言：img 是不是"可交互元素"取决于序列化器的判定，
        # 而那不在我们要验的范围内 —— 护栏只判点击目标有没有可读文本。
        print(f"\n　img 的 alt 是否进入 selector_map：{bool(find('仅alt文本'))}")

        # ── 判据 2：可读文本确实出现在 LLM 读到的文本里 ────
        blocks = _index_blocks(dom_text)
        assert blocks, f"序列化文本里一段带索引的都没有，格式假设不成立：\n{dom_text[:500]}"

        mismatched = [
            (i, t, blocks.get(i, "<该索引不在序列化结果里>"))
            for i, t in texts.items()
            if t and t not in blocks.get(i, "")
        ]
        assert not mismatched, (
            "可读文本和 LLM 实际读到的对不上（docstring 那句 'matches exactly' 不成立）：\n"
            + "\n".join(f"  [{i}] 可读={t!r}  序列化段={b!r}" for i, t, b in mismatched)
        )

        # ★ 反向探针：有没有"可读文本为空、但 LLM 明明看到了文本"的元素。
        #   这是最危险的一类 —— 规则永远匹配不上，且不报任何错。
        blind = [i for i, t in texts.items() if not t and _visible_text(blocks.get(i, ""))]
        assert not blind, (
            f"这些索引的可读文本是空的，但 LLM 在序列化结果里看得到文本：{blind}\n"
            + "\n".join(f"  [{i}] {blocks.get(i)!r}" for i in blind)
        )

        # ── 判据 3：接到真策略上 ──────────────────────────
        spec = load_task(PROJECT_ROOT / "tasks" / "pdd_search_products.yaml")
        compiled = compile_task(spec, {"keyword": "保温杯"})
        policy = compiled.policy
        url = snap["url"]

        def judge(sub: str) -> tuple[int, Any]:
            idx = find(sub)
            assert idx, f"探针页上的『{sub}』没进 selector_map，判据 3 无法成立"
            i = idx[0]
            return i, policy.evaluate("click", {"index": i}, url, texts[i])

        print("\n── 真策略对这一页的判定 ──")
        for probe in ("批量删除", "立即支付", "编辑", "搜索"):
            if not find(probe):
                print(f"  {probe}: (不在 selector_map 里)")
                continue
            _, r = judge(probe)
            print(f"  {probe}: {r.decision.value}  rule={r.rule_id or '<default>'}")

        _, r_del = judge("批量删除")
        assert r_del.is_blocked, (
            f"真策略没拦住『批量删除』：{r_del.decision.value} rule={r_del.rule_id}"
        )
        _, r_pay = judge("立即支付")
        assert r_pay.decision is Decision.CONFIRM, (
            f"『立即支付』应为 confirm，实际 {r_pay.decision.value}"
        )
        _, r_search = judge("搜索")
        assert not r_search.is_blocked, f"只读的『搜索』被拦了：rule={r_search.rule_id}"

        # ── 判据 4：快照不是不可变的 ──────────────────────
        # ★ 这条是 spike 自己踩出来的，不是计划里有的。
        #   它比前三条更"危险"，因为它的失败形态是【静默的】：
        #   一个存了 browser_state、想在最后统一处理的地方（Recorder 很容易这么写），
        #   会拿到一个空 selector_map，于是"没有任何规则命中" —— 而没有任何规则命中
        #   是一个合法结果，不报错、不告警、测试还是绿的。
        #
        # ⚠️ 第一版这里写错了：我直接读 before，结果打印出来是 0/0，断言成了空转。
        #    因为 run() 在 keep_alive 为假时【自己就会 reset】，我读的时候已经晚了。
        #    一条恒不触发的断言比没有断言更糟 —— 它会让读者以为这件事已经验过了。
        #    所以这一轮 capture 用了 keep_alive=True，才能看到 10 → 0 这个过程。
        summary = snap["summary"]
        before_dom = summary.dom_state
        before_map = summary.dom_state.selector_map
        before = len(before_map)
        aliased = agent.browser_session._cached_selector_map is before_map
        await agent.browser_session.kill()
        after = len(summary.dom_state.selector_map)

        print(f"\n── 判据 4：kill 前 selector_map={before}，kill 后={after} ──")
        print(f"   dom_state 是同一个对象: {summary.dom_state is before_dom}；"
              f"dict 是同一个对象: {summary.dom_state.selector_map is before_map}")
        print(f"   它与会话内部缓存 _cached_selector_map 是同一个 dict: {aliased}")
        if before > 0 and after == 0:
            print(
                "  → 持有的快照被【原地清空】：BrowserSession.reset() 对那份 dict 调了\n"
                "     .clear()（session.py:664），而那份 dict 就是快照里的这个对象\n"
                "     （session.py:2494 直接赋值，没有拷贝）。\n"
                "     【结论】拦截器/记录器必须在拿到 browser_state 的那【一刻】把要用的东西取走，\n"
                "     不能存下对象晚点再读。"
            )
        assert before == len(texts) > 0, (
            f"读到了 {len(texts)} 条文本，而 selector_map 只有 {before} 个 —— 计数不自洽"
        )
        assert aliased, (
            "快照的 selector_map 与会话内部缓存不是同一个 dict，"
            "那么判据 4 的机制解释（reset 原地 clear）不成立，docs 要改"
        )
        assert after == 0, f"预期 kill 之后被清空，实际还有 {after} 个 —— 结论要改"

        # ── 判据 4b（对照）：keep_alive=False 时，run() 自己就 reset 了 ──
        # ★ 没有这个对照，判据 4 会被误读成"只要不主动 kill 就没事"。
        #   而我们的 CompileTask 默认是 keep_alive=True（为了停机续跑，见 compiler.py:278），
        #   所以"哪种配置安全"这件事必须实测，不能推。
        agent2, snapshots2, _ = await _capture(site, keep_alive=False)
        snap2 = snapshots2[sorted(snapshots2)[0]]
        at_return = len(snap2["summary"].dom_state.selector_map)
        print(f"\n── 判据 4b：keep_alive=False，run() 返回时 selector_map={at_return} ──")
        print(f"   两轮抓到的文本条数：{len(texts)} vs {len(snap2['texts'])}（应相同）")
        assert len(snap2["texts"]) == len(texts), "两轮抓到的状态不可比，4b 的对照不成立"
        assert at_return == 0, (
            f"预期 run() 返回时快照已被自己 reset 清空，实际还有 {at_return} 个。"
            f"若如此，判据 4 对默认配置的结论不成立，docs 要改。"
        )


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
