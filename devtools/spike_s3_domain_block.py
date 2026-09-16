"""S3 —— Layer 0（SecurityWatchdog 域名白名单）到底挡不挡得住，以及挡住之后页面变成什么。

★ 计划里给的判据是"站外 navigate → ActionResult 含 Navigation failed，run 不崩"。
  跑 S7 时发现那还不够 —— 被拦的导航会把标签留在一个【空白页】上。
  这条如果不在设计阶段弄清楚，护栏就会有一个很难查的副作用：
  拦下一次导航 = 顺手弄丢当前页面 = 后面的提取全采到空。

★ 所以本 spike 的判据是三条，且都带对照：
  1. 站外导航被拦（错误信息里能读出是【安全策略】拦的，不是网络失败）
  2. run 不崩，后续步骤照常执行（能走到 done）
  3. 【对照】被拦之后页面是不是还是原来那个 —— 这条决定了一件事：
     Layer 0 能不能作为"拦下来之后继续干活"的机制，还是只能当"最后一道网"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_lib import local_site, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

TAG = "S3"


async def body() -> None:
    with local_site() as site:
        llm = FakeLLM(script=[
            {"navigate": {"url": site.allowed + "/"}},          # 进站
            {"navigate": {"url": site.forbidden + "/page2"}},   # 出站（同服务器，换主机名）
            {"extract": {"query": "页面上有什么"}},              # 出站被拦之后还能不能干活
            {"done": {"text": "结束", "success": True}},
        ])
        agent = make_agent(llm, task="进站、尝试出站、然后提取", allowed_domains=["127.0.0.1"])
        history = await agent.run(max_steps=6)
        await agent.browser_session.kill()

        urls = [u or "" for u in history.urls()]
        errors = [r.error for step in history.history for r in (step.result or []) if r.error]
        print("每一步的 URL:")
        for i, u in enumerate(urls):
            print(f"  step {i}: {u}")
        print("错误:")
        for e in errors:
            print("  ", e[:200])

        # 判据 1：被拦，且原因可读
        blocked = [e for e in errors if "blocked by security policy" in e]
        assert blocked, f"站外导航没被拦（或错误信息换了说法）。errors={errors}"
        assert site.forbidden not in urls, f"站外地址居然进了历史。urls={urls}"

        # 判据 2：run 没崩，走到了最后
        assert history.is_done(), "被拦之后 run 中断了 —— 护栏不该让任务崩掉"
        assert history.final_result() == "结束", f"没走到 done：{history.final_result()!r}"

        # 判据 3（对照）：被拦之后页面在哪
        after = urls[2] if len(urls) > 2 else ""
        print(f"\n【关键发现】被拦之后的页面: {after!r}")
        if after == "about:blank":
            print("  → 被拦的导航把标签留在了空白页：Layer 0 是「最后一道网」，")
            print("    不能当「拦下来之后继续干活」的机制。Layer 1 必须在动作执行【前】拦，")
            print("    因为那样根本不产生导航，页面状态不会被破坏。")
        assert after, "拿不到被拦之后的 URL，这条结论无法成立"


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
