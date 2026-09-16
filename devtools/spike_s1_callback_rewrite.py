"""S1 —— 护栏的核心机制：能不能在 new_step_callback 里改写 LLM 的动作？

★ 这是全项目风险最高的一条（计划里的 R1）。
  护栏的"人工拒绝后让 LLM 改道"依赖三件事同时成立：
    1. register_new_step_callback 真的会被调用（未文档化）
    2. 回调里改写 agent_output.action 真的会生效（它得是同一个对象）
    3. await 人工审批期间浏览器还活着、且不受 Tools.act 的 180s 超时约束

  ★ 判据怎么定才有效：
    "回调被调用了"不能作为成功判据 —— 被调用但改写无效，护栏就是个安慰剂。
    所以判据是【最终 URL 落在被改写到的那个页面上】，
    即 history.urls() 里出现 /page2，而 LLM 脚本里从没说过要去 /page2。

  真去 /page2 有两条独立路径：LLM 的脚本 vs 我们的改写。
  脚本写的是 navigate 到 /（首页），改写把它换成 /page2 ——
  如果只有回调没生效，URL 会是 /。所以这个判据能把两者分开。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_lib import local_site, make_action, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

TAG = "S1"


async def body() -> None:
    with local_site() as site:
        # LLM 只想打开首页。改写会把它换成 /page2。
        llm = FakeLLM(script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"done": {"text": "结束", "success": True}},
        ])
        agent = make_agent(llm, task="打开页面然后结束", allowed_domains=["127.0.0.1"])

        seen: list[str] = []
        heartbeats: list[int] = []
        rewritten = 0

        async def on_new_step(browser_state, model_output, step_index):  # noqa: ANN001
            """★ 时序：LLM 输出之后、action 执行之前（已核实的源码事实 5）。

            ★★ 这里必须【按条件】改写，不能无条件改写。第一次跑这个 spike 时我
              写的是无条件替换，结果 `done` 也被换成了 navigate ——
              于是 run 永远结束不了，只能靠 max_steps 或脚本耗尽收场。
              这个坑在接真拦截器时一模一样：拦截器必须只改策略判定要改的动作，
              "顺手全改一遍"看起来更安全，实际是让 agent 再也完不成任务。

              所以本 spike 的第二个断言就是：`done` 必须原样通过。
            """
            nonlocal rewritten
            actions = [a.model_dump(exclude_unset=True) for a in model_output.action]
            seen.append(f"step={step_index} actions={actions}")

            if any("navigate" in a for a in actions):
                rewritten += 1
                model_output.action = [
                    make_action(agent, "navigate", url=site.allowed + "/page2")
                ]

        async def heartbeat():
            """★ 独立协程，证明回调里 await 期间事件循环还在转（浏览器心跳不断）。

            这条对应计划里的 R3：审批若发生在 Tools.act 的 action 函数体内，
            会撞上 180s 的 asyncio.wait_for。放在 new_step_callback 里就不受它约束 ——
            而"不受约束"的前提正是这里验的：回调里的 await 不会把事件循环卡住。
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

        urls = history.urls()
        print("回调调用次数:", len(seen))
        for line in seen[:3]:
            print("  ", line)
        print("被改写的步数:", rewritten)
        print("history.urls():", urls)
        print("心跳在这期间跳了:", len(heartbeats), "次")

        assert seen, "register_new_step_callback 从未被调用 —— 护栏的首选机制不存在"
        assert any("/page2" in (u or "") for u in urls), (
            f"改写 agent_output.action 没生效：URL 里没有 /page2，实际 {urls}"
        )
        assert len(heartbeats) > 1, "回调执行期间事件循环没有继续转"

        # ★ 第二条判据：只有该改的被改。
        #   第一次跑时这里是无条件改写，`done` 也被换成 navigate，
        #   于是这个 run 是靠脚本耗尽才停的 —— 而断言 1 照样会通过。
        #   一条会放过"把任务搞死"的断言，等于没断言。
        assert rewritten == 1, f"预期只改写 1 步，实际 {rewritten} 步。回调改多了。"
        assert history.is_done() and history.final_result() == "结束", (
            f"done 没能穿过回调 —— agent 已经无法正常结束任务了。"
            f"is_done={history.is_done()}, final={history.final_result()!r}"
        )


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
