"""S5 —— 截图两条路都能拿到【真 PNG】吗？以及库自己落的图在哪。

★ 为什么必须验"是不是真 PNG"而不是"有没有截图"：
    "有没有"极易满足 —— 一段报错文本、一个被转坏的 base64、一个 0 字节文件，
    都能让 `screenshot is not None` 成立。而这些图是亮点 4（可观测回放）的最终交付物，
    图坏掉必须在【这一层】发现。等打开 report.html 才看到坏图时，
    已经不知道是哪一步、哪条路径坏的了。

★ 两条路各自服务谁（这是设计的由来，不是随便选的）：
    · new_step_callback 的 browser_state.screenshot —— 回调里【就在手边】，
      不碰磁盘。护栏/记录器当场要判断"这一步页面变没变"时用它最省。
    · on_step_end 的 history[-1].state.screenshot_path —— 库已经把图落盘了，
      给我们一个路径。要长期留档时用它少一次编码往返。
  但库落在【系统临时目录】（service.py:448-449），关机即失，
  所以审计必须自己另存一份。这条如果只在文档里写"要另存"，没人会当真；
  下面把它变成一条可执行的断言。

★ 判据：
    1. 回调那条路：至少一步有截图，且解出来的字节是 PNG 魔数
    2. 钩子那条路：至少一步有路径、文件存在、磁盘上的字节是 PNG 魔数
    3. 【两路一致】两条路拿到的图，sha256 集合必须相同
       —— 记录器会用回调的图存内存、用钩子的路径写报告，
          两者若不一致，报告和库里就是两帧不同的画面，而且没人会发现
    4. 【ADR-2 的证据】use_vision=False 下截图照样采集，但一张都没发给模型
       —— "采集了截图" 不能推出 "把截图发给了模型"，这两件事必须分开验
    5. 【设计依据】库落的图不在项目目录下，覆盖不了"审计要自己另存"
"""
from __future__ import annotations

import base64
import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_lib import local_site, looks_like_png, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

TAG = "S5"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


async def body() -> None:
    with local_site() as site:
        llm = FakeLLM(script=[
            {"navigate": {"url": site.allowed + "/"}},
            {"scroll": {"down": True, "pages": 1.0}},
            {"done": {"text": "结束", "success": True}},
        ])
        agent = make_agent(
            llm,
            task="打开页面、滚动一下、结束",
            # ★ use_vision=False 是我们的生产配置（ADR-2：零 VLM，纯文本 DOM 树驱动）
            agent_kw={"use_vision": False},
            allowed_domains=["127.0.0.1"],
            # ★ keep_alive=True：让库的临时目录活到断言之后，好去磁盘上核那几张图。
            #   这也解释了为什么必须留它活着 —— 默认配置下 run() 一返回就把浏览器
            #   和中间产物都收了，那时再去读路径只会得到"文件不存在"。
            keep_alive=True,
        )

        # 路 A：回调里的 base64
        from_callback: dict[int, str | None] = {}
        # 路 B：钩子里的磁盘路径
        from_hook: list[tuple[int, str | None, bool]] = []

        async def on_new_step(browser_state: Any, model_output: Any, step_index: int) -> None:
            from_callback[step_index] = browser_state.screenshot

        async def on_step_end(agent: Any) -> None:
            h = agent.history.history
            p = h[-1].state.screenshot_path if h else None
            from_hook.append((len(h), p, bool(p and Path(p).exists())))

        agent.register_new_step_callback = on_new_step
        history = await agent.run(max_steps=4, on_step_end=on_step_end)

        # ── 判据 1：回调那条路 ────────────────────────────
        print("── 路 A：new_step_callback 的 screenshot（base64）──")
        cb_sha: dict[int, str] = {}
        for step in sorted(from_callback):
            b64 = from_callback[step]
            if not b64:
                print(f"  step {step}: None")
                continue
            raw = base64.b64decode(b64)
            cb_sha[step] = _sha(raw)
            print(f"  step {step}: len(b64)={len(b64)}  解出 {len(raw)} 字节  "
                  f"PNG={looks_like_png(raw)}  sha256={cb_sha[step][:12]}")

        assert cb_sha, "回调里一步截图都没拿到 —— 可观测性的主路径不成立"
        bad = [s for s, b64 in from_callback.items() if b64 and not looks_like_png(base64.b64decode(b64))]
        assert not bad, f"这些步的截图不是 PNG：{bad}"

        # ── 判据 2：钩子那条路 ────────────────────────────
        print("\n── 路 B：on_step_end 的 history[-1].state.screenshot_path ──")
        hook_sha: list[str] = []
        for n, p, exists in from_hook:
            if not p:
                print(f"  第 {n} 次钩子调用: 该步无截图路径")
                continue
            if not exists:
                print(f"  第 {n} 次钩子调用: 路径存在但文件不在 → {p}")
                continue
            raw = Path(p).read_bytes()
            hook_sha.append(_sha(raw))
            print(f"  第 {n} 次钩子调用: {len(raw)} 字节  PNG={looks_like_png(raw)}  "
                  f"sha256={hook_sha[-1][:12]}\n      {p}")

        assert hook_sha, "钩子里一条可用的截图路径都没有 —— 报告那条路不成立"
        assert all(
            looks_like_png(Path(p).read_bytes()) for _, p, ok in from_hook if ok and p
        ), "钩子给出的路径里，有文件不是 PNG"

        # ── 判据 3：两条路拿到的图必须是同一批 ────────────
        # ★ 不做这个对照，两条路各修各的 bug：回调那条一直好好的，钩子那条
        #   可能因为 off-by-one 落后一步。两边单测都绿，而报告里的图是错帧的。
        print("\n── 判据 3：两路的 sha256 集合对照 ──")
        print(f"  路 A: {sorted(v[:12] for v in cb_sha.values())}")
        print(f"  路 B: {sorted(v[:12] for v in hook_sha)}")
        assert sorted(cb_sha.values()) == sorted(hook_sha), (
            "两条路给出的图不是同一批 —— 记录器用 A、报告用 B 的话，两边会是不同的帧。\n"
            f"  A={sorted(v[:12] for v in cb_sha.values())}\n"
            f"  B={sorted(v[:12] for v in hook_sha)}"
        )

        # ── 判据 4：ADR-2 的可执行证据 ────────────────────
        # ★ 这条把两件事分开了：
        #     "截图被采集了"（判据 1/2 已证）与 "截图被发给了模型"（本条证否）。
        #   合成一条的话，将来有人把 use_vision 打开，截图那条断言照样绿，
        #   而成本已经翻倍了（每步一张图进提示词）。
        print("\n── 判据 4：use_vision=False 下，图采集了但一张都没发给模型 ──")
        img_parts = llm.total_image_parts()
        print(f"  采集到的截图数: {len(cb_sha)}；步进调用里的图片分片总数: {img_parts}")
        assert img_parts == 0, (
            f"use_vision=False 下仍有 {img_parts} 个图片分片进了提示词 —— "
            f"要么配置没生效，要么库改了行为，ADR-2 的成本论证要重算"
        )

        # ── 判据 5：库落的图在哪（决定"必须自己另存"）────
        print("\n── 判据 5：库自己把图落在哪 ──")
        lib_paths = [p for _, p, ok in from_hook if ok and p]
        tmp = Path(tempfile.gettempdir())
        for p in lib_paths:
            print(f"  {p}\n    在项目目录下？{Path(p).is_relative_to(PROJECT_ROOT)}"
                  f"   在系统临时目录下？{Path(p).is_relative_to(tmp)}")
        assert lib_paths and all(Path(p).is_relative_to(tmp) for p in lib_paths), (
            "库落的截图不在系统临时目录下 —— 那么'关机即失、审计必须自己另存'"
            "这个设计前提要重新核实（可能库改了存储位置）"
        )
        print("  → 全在系统临时目录：关机/清理即失。审计必须自己另存一份。")

        # ── 判据 6：库自己的访问器给的是同一批路径 ────────
        via_history = [p for p in history.screenshot_paths() if p]
        assert sorted(via_history) == sorted(lib_paths), (
            f"history.screenshot_paths() 和钩子里看到的对不上：{via_history} vs {lib_paths}"
        )
        print(f"\n── 判据 6：history.screenshot_paths() 给出同样的 {len(via_history)} 条路径 ──")

        # ── 判据 7：连续两步 sha 相同 = "画面没变"，但它是【原始信号】，不是结论 ──
        # ★ 这一条是跑出来的意外收获。计划里写的是"连续两步 sha 相同 →
        #   报告里标注『页面未变化（可能是点击无效）』"。实测发现这个标注下早了：
        #
        #   本例 step 2 与 step 3 的 sha 完全相同，而 step 2 的动作是 scroll ——
        #   日志明写 "🔍 Scrolled down 1080px"，它【成功执行了】，
        #   只是这个 mock 页面比视口还短，滚不动，画面自然没变。
        #   于是"画面没变"在这里是【完全正常】的，而同一句话用在 click 上就是可疑的。
        #
        #   结论：sha 相同只是"画面没变"这个原始事实，要能和动作名一起看才有意义。
        #   报告里必须把动作名和这个标注并排显示，否则读者会把正常行为当故障查。
        same_frame = len(set(cb_sha.values())) == 1
        acts: list[tuple[int, list[str], list[str]]] = []
        for i, h in enumerate(history.history):
            names = [
                next(iter(a.model_dump(exclude_unset=True)))
                for a in (h.model_output.action if h.model_output else [])
            ]
            errs = [r.error for r in (h.result or []) if r.error]
            acts.append((i + 1, names, errs))

        print("\n── 判据 7：画面变化 vs 动作名 ──")
        for step, names, errs in acts:
            shot = cb_sha.get(step)
            print(f"  step {step}: 动作={names} 截图sha={(shot or '<无>')[:12]} 错误={errs}")

        if same_frame:
            assert len(cb_sha) > 1, "只有一步有截图，'连续两步相同'无从谈起"
            scroll_steps = [s for s, names, _ in acts if "scroll" in names]
            assert scroll_steps, (
                f"预期本例的相同画面来自 scroll 步，实际动作序列是 {[n for _, n, _ in acts]}"
            )
            assert not any(errs for _, _, errs in acts), (
                f"有步骤报错了 —— 那'画面没变'就不是正常行为，本判据的结论不成立：{acts}"
            )
            print(
                "  → 所有步画面相同，但 scroll 【执行成功且无错误】（日志: Scrolled down 1080px）。\n"
                "     页面比视口短，本来就没得滚 —— 所以这里是【真阳性】：\n"
                "     信号说的是'画面没变'，而这件事是正常还是可疑，取决于动作是什么。\n"
                "     【结论】sha 相同必须和动作名并排展示，不能直接标注成'点击无效'。"
            )

        await agent.browser_session.kill()


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
