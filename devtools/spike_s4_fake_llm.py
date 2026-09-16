"""S4 —— 不继承任何东西的 FakeLLM 能被 Agent 接受吗？以及"一次 run = 几次 LLM 调用"。

★ 前半段（能不能跑）是计划里写的判据。后半段是搭桩过程中撞出来的，而且更值钱：
  我们一直默认"一个 run 有几个 step 就是几次 LLM 调用"。实测不是 ——
  库在 run 末尾会额外发一次 judge 调用，而 extract 动作内部还会再发一次。
  对一个按 token 计费的项目，"我以为 3 次、实际 5 次"是必须在架构阶段就知道的事。

★ 判据：
    1. 纯鸭子类型成立：FakeLLM 的 MRO 里除了 object 没有别的基类
       —— 把它写成断言，是因为"我没继承"这件事会被人无意改掉
    2. 一个脚本化的 run 能跑完并走到 done，URL 证明脚本真的驱动了浏览器
    3. 【成本账】非步进调用至少 2 次：run 末尾的 judge + extract 的内部调用
    4. use_vision=False 时提示词里零图片分片
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_lib import local_site, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

TAG = "S4"


async def body() -> None:
    # ── 判据 1：纯鸭子类型，零继承 ────────────────────────
    bases = [b.__name__ for b in FakeLLM.__mro__[1:]]
    print(f"FakeLLM.__mro__[1:] = {bases}")
    assert bases == ["object"], (
        f"FakeLLM 多了基类 {bases} —— 桩一旦继承库的内部基类，库改基类就会让桩失效，"
        f"而那正是这个桩被设计成鸭子类型要避免的事"
    )

    with local_site() as site:
        llm = FakeLLM(script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"extract": {"query": "页面上有什么"}},
            {"done": {"text": "结束", "success": True}},
        ])
        agent = make_agent(
            llm,
            task="打开页面、抽取一次、然后结束",
            agent_kw={"use_vision": False},
            allowed_domains=["127.0.0.1"],
            keep_alive=True,
        )
        history = await agent.run(max_steps=6)

        # ── 判据 2：脚本真的驱动了浏览器 ──────────────────
        urls = [u or "" for u in history.urls()]
        print(f"\nhistory.urls(): {urls}")
        print(f"步进调用（脚本游标）次数: {llm.steps}；耗尽: {llm.exhausted}")
        assert history.is_done(), "run 没走到 done"
        assert history.final_result() == "结束", f"final_result={history.final_result()!r}"
        assert any(site.allowed in u for u in urls), (
            f"脚本里的 navigate 没生效 —— URL 历史里没有站点地址。urls={urls}"
        )
        assert not llm.exhausted, (
            "脚本被跑穿了（步数比脚本长）—— 说明有一步没按预期消耗脚本"
        )

        # ── 判据 3：成本账 —— 非步进调用 ──────────────────
        # ★ 分组而不是只数个数：知道"多了 2 次"不够，
        #   得知道多的是【什么】—— judge 是每次 run 固定一次，
        #   extract 是按动作次数可重复的。两者的增长方式完全不同。
        #
        # ⚠️ 第一版这里写的是 `type(output_format).__name__`，结果 judge 被归成了
        #   "ModelMetaclass" —— 因为 output_format 是个【类】，
        #   而 `type(类)` 是它的元类。统计代码本身出错时不会报错，
        #   只会安静地把东西归到一栏你从没见过的名字下面。所以这里显式区分。
        def _kind(output_format: object) -> str:
            if output_format is None:
                return "<抽取类: output_format=None>"
            return getattr(output_format, "__name__", type(output_format).__name__)

        kinds = Counter(_kind(c.kwargs.get("output_format")) for c in llm.other_calls)
        print("\n── 非步进 LLM 调用明细 ──")
        for kind, n in sorted(kinds.items()):
            print(f"  {kind}: {n} 次")
        extract_calls = kinds.get("<抽取类: output_format=None>", 0)
        judge_calls = kinds.get("JudgementResult", 0)
        total = llm.steps + len(llm.other_calls)
        print(f"  合计: 步进 {llm.steps} 次 + 非步进 {len(llm.other_calls)} 次 = {total} 次")

        assert judge_calls >= 1, (
            f"没看到 run 末尾的 judge 调用（use_judge 默认 True，views.py:74）—— "
            f"要么库改了默认值，要么它走了别的 llm 实例。实际 kinds={dict(kinds)}"
        )
        assert extract_calls >= 1, (
            f"extract 动作没有引发额外的 LLM 调用 —— 那么'抽取不是免费的'这条结论不成立。"
            f"实际 kinds={dict(kinds)}"
        )

        # ── 判据 4：零图片分片 ────────────────────────────
        assert llm.total_image_parts() == 0, (
            f"use_vision=False 下仍有 {llm.total_image_parts()} 个图片分片进了提示词"
        )
        print(f"\n提示词里的图片分片总数: {llm.total_image_parts()}（use_vision=False）")

        print(
            f"\n【结论】这个 run 只有 {llm.steps} 步，却发了 {total} 次 LLM 请求。\n"
            f"  · judge：每次 run 固定 +1（agent/service.py:1620，use_judge 默认开）\n"
            f"  · extract：每调用一次 +1（tools/service.py:1197，不是读页面而是问模型）\n"
            f"  → 所以采集走结构化 done 而不是 extract，不只是'更结构化'，也是'更省'。"
        )

        await agent.browser_session.kill()


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
