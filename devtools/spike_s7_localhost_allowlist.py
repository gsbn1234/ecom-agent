"""S7 —— allowed_domains=["127.0.0.1"] 能不能匹配 http://127.0.0.1:PORT？

★ 为什么这条必须先验：Phase 4 的本地 mock 卖家后台就跑在 127.0.0.1 上。
  如果白名单匹配不了带端口的 IP 主机名，整个 e2e 会在"连站点都进不去"上卡住，
  而报错会指向"导航失败"，看起来像浏览器问题。

★ 判据必须带对照组（这是本项目的招牌手法）：
  光断言"能打开 127.0.0.1 的页面"是不够的 —— 白名单要是压根没生效，
  这条断言照样通过。所以同一次运行里还要断言
  【同一台服务器、同一端口、换个主机名 localhost 就打不开】。
  两个断言同时成立，才能说明白名单真的在按域名判定。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_lib import local_site, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

TAG = "S7"


async def body() -> None:
    with local_site() as site:
        llm = FakeLLM(script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"navigate": {"url": site.forbidden + "/"}},   # 同一台服务器，只换主机名
            {"done": {"text": "结束", "success": True}},
        ])
        agent = make_agent(
            llm,
            task="依次打开两个地址",
            allowed_domains=["127.0.0.1"],   # 只允许 IP，不允许 localhost
        )
        history = await agent.run(max_steps=5)
        await agent.browser_session.kill()

        urls = [u or "" for u in history.urls()]
        print("访问过的 URL:", urls)
        errors = [r.error for step in history.history for r in (step.result or []) if r.error]
        for e in errors:
            print("  ActionResult.error:", e[:300])

        assert any("127.0.0.1" in u for u in urls), (
            f"白名单里的 127.0.0.1 打不开 —— mock 站点方案不成立。urls={urls}"
        )
        # ★ 对照：同一个服务器，只是主机名不同，必须进不去
        assert not any("localhost" in u for u in urls), (
            f"localhost 也进去了 —— 白名单没在按域名判定，S3 的结论会不可信。urls={urls}"
        )


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
