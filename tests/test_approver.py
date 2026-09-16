"""审批通道：四种实现 + fail-closed 保证。

★ 这个文件里最重要的不是"批准能走通"（那条路径谁都会测），
  而是【所有答不上来的情形都必须变成拒绝】：
  超时、通道抛异常、stdin 不可用、决策文件写坏了、决策文件根本不存在。
  每一条都要有对照实验 —— 否则"没批准"可能只是因为请求压根没发出去。
"""
import asyncio
import json
import warnings

import pytest

from ecom_agent.guardrails.approver import (
    DENIED_ERROR_PREFIX,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResult,
    AutoApproveApprover,
    AutoDenyApprover,
    BaseApprover,
    CliApprover,
    FileApprover,
    WebApprover,
    get_approver,
)


def _req(**kw) -> ApprovalRequest:
    base = dict(
        run_id="run-1",
        step=3,
        action_name="click",
        params={"index": 12},
        element_text="立即支付",
        url="https://mms.pinduoduo.com/goods/goods_list",
        rule_id="confirm-money",
        reason="资金相关，必须人工二次确认",
    )
    return ApprovalRequest(**{**base, **kw})


# ── AutoDeny：护栏失败路径的常规测法 ──────────────────────
def test_auto_deny_denies():
    r = asyncio.run(AutoDenyApprover().request(_req()))
    assert r.outcome is ApprovalOutcome.DENIED
    assert r.deny() and not r.approved
    assert r.approved_by == "auto-deny"


def test_denial_error_is_addressed_to_the_llm():
    """★ 这句话的读者是 LLM，不是人。

    它必须说清三件事：被拒了、为什么、以及【不要重复这个动作】。
    少了最后一句，LLM 最可能的反应是换个说法再试一次同一个危险动作。
    """
    req = _req()
    r = asyncio.run(AutoDenyApprover().request(req))
    err = r.to_action_error(req)
    assert err.startswith(DENIED_ERROR_PREFIX), "前缀是契约，报告和测试都 grep 它"
    assert "不要重复这个动作" in err
    assert "confirm-money" in err, "规则 id 要在里面，便于 LLM 和人定位"
    assert "立即支付" in err


def test_timeout_message_is_distinguishable_from_denial():
    """★ 超时和"人点了拒绝"对 LLM 的指示相同（都别做了），
    但对审计者的含义完全不同 —— 一个是护栏在工作，一个是审批通道没人管。
    """
    req = _req()
    timeout = ApprovalResult(ApprovalOutcome.TIMEOUT, note="超过 300s 无人应答")
    error = ApprovalResult(ApprovalOutcome.ERROR, note="磁盘满了")
    assert "超时" in timeout.to_action_error(req)
    assert "通道故障" in error.to_action_error(req)
    assert timeout.to_action_error(req) != error.to_action_error(req)


# ── fail-closed 的五条路径 ────────────────────────────────
class _Hanging(BaseApprover):
    """一个永远不回答的通道 —— 用来测超时，不依赖任何真实通道。"""

    async def _decide(self, req):  # noqa: ANN001
        await asyncio.sleep(3600)
        raise AssertionError("不该走到这里")


def test_timeout_is_denied_not_approved():
    """★★ 全项目最重要的一条 fail-closed 断言。

    超时如果被当成放行，护栏就退化成一个"没人看着的时候自动全放"的开关 ——
    比没有护栏更糟，因为它看起来是有的。
    """
    r = asyncio.run(_Hanging(timeout_s=0.05).request(_req()))
    assert r.outcome is ApprovalOutcome.TIMEOUT
    assert r.deny()


class _Broken(BaseApprover):
    async def _decide(self, req):  # noqa: ANN001
        raise OSError("磁盘满了")


def test_channel_exception_is_denied_not_approved():
    """通道自己坏了（磁盘满、权限错、网络断）也必须拒绝，而不是让异常冒出去。

    冒出去的话，调用方（拦截器）要么崩掉整个 run，要么更糟 ——
    被 try/except 顺手吞掉然后继续执行那个危险动作。
    """
    r = asyncio.run(_Broken().request(_req()))
    assert r.outcome is ApprovalOutcome.ERROR
    assert r.deny()
    assert "OSError" in r.note, "故障原因要留下，否则运维无从查起"


class _Cancelled(BaseApprover):
    async def _decide(self, req):  # noqa: ANN001
        raise asyncio.CancelledError


def test_cancellation_propagates_instead_of_being_swallowed():
    """★★ 对照实验：CancelledError 是 BaseException，不该被 fail-closed 逻辑吃掉。

    吃掉的后果是"停止 run"变成一个失效的按钮 —— 界面显示已停止，
    协程还在跑，浏览器还在动。而这恰恰是最需要它生效的场景。
    """
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_Cancelled().request(_req()))


def test_run_id_is_filled_in_from_the_approver():
    """拦截器不该被迫给每条请求手填 run_id —— 通道自己知道自己在哪个 run 里。"""
    req = _req(run_id="")
    r = asyncio.run(AutoDenyApprover(run_id="run-42").request(req))
    assert r.deny()
    # 原对象不被就地修改（ApprovalRequest 是 pydantic 模型，就地写会污染调用方）
    assert req.run_id == ""


# ── Web 通道 ──────────────────────────────────────────────
def test_web_approver_waits_until_resolved(tmp_path):
    """正常路径：请求挂起 → Web 层 resolve → 被唤醒。"""

    async def main():
        ap = WebApprover(pending_dir=tmp_path / "p", decided_dir=tmp_path / "d", timeout_s=5)
        req = _req()
        task = asyncio.create_task(ap.request(req))
        await asyncio.sleep(0.05)  # 让 _decide 跑到 await event.wait()

        assert ap.waiting() == [req.id], "等待中的审批要能被 Web 层列出来"
        assert (tmp_path / "p" / f"{req.id}.json").is_file(), "pending 文件是 Web 卡片的数据源"

        assert ap.resolve(req.id, True, approved_by="tester", note="看过了") is True
        return req, await task

    req, result = asyncio.run(main())
    assert result.outcome is ApprovalOutcome.APPROVED
    assert result.approved_by == "tester"
    assert result.note == "看过了"
    # 决策后 pending 被移走，decided 留下含原请求的完整流水
    assert not (tmp_path / "p" / f"{req.id}.json").exists()
    archived = json.loads((tmp_path / "d" / f"{req.id}.json").read_text(encoding="utf-8"))
    assert archived["result"]["outcome"] == "denied" or archived["result"]["outcome"] == "approved"
    assert archived["result"]["approved"] is True
    assert archived["element_text"] == "立即支付", "流水里要留着当时到底点了什么"


def test_web_approver_denial(tmp_path):
    async def main():
        ap = WebApprover(pending_dir=tmp_path / "p", decided_dir=tmp_path / "d", timeout_s=5)
        req = _req()
        task = asyncio.create_task(ap.request(req))
        await asyncio.sleep(0.05)
        ap.resolve(req.id, False, approved_by="tester", note="不许动钱")
        return req, await task

    req, result = asyncio.run(main())
    assert result.outcome is ApprovalOutcome.DENIED
    assert DENIED_ERROR_PREFIX in result.to_action_error(req)
    assert "不许动钱" in result.to_action_error(req)


def test_web_approver_timeout_cleans_up_pending_file(tmp_path):
    """★ 超时后 pending/ 里不能留下孤儿文件。

    留着的话，Web 界面会一直显示一张"等待中"的卡片，而那个 run 早就结束了 ——
    人会点它，然后拿到一个"不在等待中"的警告，从此不再相信这个界面。
    顺带：_events 也必须清掉，否则每超时一次就泄漏一个 Event。
    """

    async def main():
        ap = WebApprover(pending_dir=tmp_path / "p", decided_dir=tmp_path / "d", timeout_s=0.1)
        req = _req()
        result = await ap.request(req)
        return ap, req, result

    ap, req, result = asyncio.run(main())
    assert result.outcome is ApprovalOutcome.TIMEOUT
    assert not (tmp_path / "p" / f"{req.id}.json").exists(), "超时要清掉 pending"
    assert ap.waiting() == [], "_events 不能泄漏"


def test_resolve_after_timeout_returns_false(tmp_path):
    """迟到的决策不能唤醒一个已经不存在的等待。

    返回 False 而不是静默 True —— Web 层要能告诉用户"这条已经过期了"。
    """

    async def main():
        ap = WebApprover(pending_dir=tmp_path / "p", decided_dir=tmp_path / "d", timeout_s=0.1)
        req = _req()
        await ap.request(req)
        return ap.resolve(req.id, True)

    assert asyncio.run(main()) is False


def test_unresolved_requests_survive_in_pending_dir(tmp_path):
    """★ pending/ 非空 = 上一个 run 在等审批时死了。这是刻意的诊断信号。

    对照：走完正常流程后决定不能留残留 —— 见上面 test_web_approver_waits_until_resolved。
    """

    async def main():
        ap = WebApprover(pending_dir=tmp_path / "p", decided_dir=tmp_path / "d", timeout_s=5)
        task = asyncio.create_task(ap.request(_req()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    leftovers = list((tmp_path / "p").glob("*.json"))
    assert len(leftovers) == 1, "被取消的请求留下的 pending 文件正是我们要的'上次死在这'信号"


# ── File 通道 ─────────────────────────────────────────────
def test_file_approver_reads_decision_from_disk(tmp_path):
    """跨进程通信：决策由"另一个进程"写出，通道轮询到它。"""

    async def main():
        ap = FileApprover(
            pending_dir=tmp_path / "p",
            decided_dir=tmp_path / "d",
            timeout_s=5,
            poll_interval_s=0.02,
        )
        req = _req()
        task = asyncio.create_task(ap.request(req))
        await asyncio.sleep(0.05)
        assert (tmp_path / "p" / f"{req.id}.json").is_file(), "请求要落到 pending 供人查看"
        (tmp_path / "d" / f"{req.id}.json").write_text(
            json.dumps({"approved": True, "approved_by": "ci", "note": "自动放行一次"}),
            encoding="utf-8",
        )
        return await task

    r = asyncio.run(main())
    assert r.outcome is ApprovalOutcome.APPROVED
    assert r.approved_by == "ci"


def test_file_approver_timeout_denies(tmp_path):
    async def main():
        return await FileApprover(
            pending_dir=tmp_path / "p",
            decided_dir=tmp_path / "d",
            timeout_s=0.1,
            poll_interval_s=0.02,
        ).request(_req())

    r = asyncio.run(main())
    assert r.outcome is ApprovalOutcome.TIMEOUT
    assert r.deny()


def test_file_approver_malformed_decision_is_denied(tmp_path):
    """★★ 决策文件写坏了（写了一半、编码错、不是 JSON）→ 必须拒绝。

    这是最危险的一类输入：人在赶时间的时候手工 echo 一个文件出来，
    内容不对是常事。而"解析失败"最容易被顺手写成 return approved=False...
    或者更糟 —— 有人为了"别卡住"改成 return True。
    """

    async def main():
        ap = FileApprover(
            pending_dir=tmp_path / "p",
            decided_dir=tmp_path / "d",
            timeout_s=2,
            poll_interval_s=0.02,
        )
        req = _req()
        task = asyncio.create_task(ap.request(req))
        await asyncio.sleep(0.05)
        (tmp_path / "d" / f"{req.id}.json").write_text("{ 这不是 JSON", encoding="utf-8")
        return await task

    r = asyncio.run(main())
    assert r.outcome is ApprovalOutcome.ERROR
    assert r.deny(), "读不懂 ≠ 放行"


def test_file_approver_missing_approved_key_is_denied(tmp_path):
    """JSON 合法但缺 approved 字段 → 同样拒绝（KeyError 被 fail-closed 接住）。"""

    async def main():
        ap = FileApprover(
            pending_dir=tmp_path / "p",
            decided_dir=tmp_path / "d",
            timeout_s=2,
            poll_interval_s=0.02,
        )
        req = _req()
        task = asyncio.create_task(ap.request(req))
        await asyncio.sleep(0.05)
        (tmp_path / "d" / f"{req.id}.json").write_text('{"ok": true}', encoding="utf-8")
        return await task

    assert asyncio.run(main()).deny()


# ── CLI 通道 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "typed,should_approve",
    [("y", True), ("Y", True), ("yes", True), ("是", True), ("n", False), ("", False), ("随便打的", False)],
)
def test_cli_answers(monkeypatch, typed, should_approve):
    """★ 默认拒绝：空回车、乱输入都是拒绝。

    对照实验的意义在 ("", False)：如果把默认值写成 Y，那么"人没看清就回车"
    就等于放行 —— 而这个通道的存在意义正是"要有人明确点头"。
    """
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)
    r = asyncio.run(CliApprover(timeout_s=5).request(_req()))
    assert r.approved is should_approve


def test_cli_timeout_denies(monkeypatch):
    """人不回答 → 拒绝，且不能把事件循环卡死。

    ★ 桩必须是【同步】阻塞函数。用 async def 写桩会立刻返回一个协程对象，
      于是 to_thread 根本没被阻塞 —— 测试会"通过"，但它验证的是另一件事
      （而且协程没被 await 时只留一条 RuntimeWarning，很容易被忽略）。
    """

    def _slow_input(prompt=""):
        import time

        time.sleep(0.5)  # 在 to_thread 的线程里真的阻塞，模拟人盯着屏幕不动
        return "y"

    monkeypatch.setattr("builtins.input", _slow_input)
    r = asyncio.run(CliApprover(timeout_s=0.1).request(_req()))
    assert r.outcome is ApprovalOutcome.TIMEOUT
    assert r.deny(), "就算人最后按了 y，超过超时也不算数"


def test_cli_without_stdin_reports_error_not_denied(monkeypatch):
    """★ stdin 不可用时（CI、管道重定向）报 ERROR 而不是 DENIED。

    差别在可操作性：DENIED 会让人以为"有人拒绝了"，
    而真相是"这个通道在无头环境里根本不能用，该换成 FileApprover"。
    """

    def _eof(prompt=""):
        raise EOFError("stdin 关了")

    monkeypatch.setattr("builtins.input", _eof)
    r = asyncio.run(CliApprover(timeout_s=5).request(_req()))
    assert r.outcome is ApprovalOutcome.ERROR
    assert "stdin" in r.note


def test_cli_does_not_block_the_event_loop(monkeypatch):
    """★★ input() 必须走 to_thread —— 直接调用会阻塞事件循环。

    这条用一个"心跳"协程来验：审批挂起期间，心跳必须照常跳动。
    直接阻塞事件循环的话心跳会停 —— 而在真实 run 里，那个心跳就是 CDP 连接，
    停了意味着浏览器会掉线，表现为"审批完之后浏览器就坏了"。
    """
    import threading

    release = threading.Event()
    monkeypatch.setattr("builtins.input", lambda prompt="": (release.wait(5), "y")[1])

    async def main():
        beats = 0

        async def heartbeat():
            nonlocal beats
            while True:
                await asyncio.sleep(0.01)
                beats += 1

        hb = asyncio.create_task(heartbeat())
        task = asyncio.create_task(CliApprover(timeout_s=5).request(_req()))
        await asyncio.sleep(0.2)
        beats_while_waiting = beats
        release.set()
        result = await task
        hb.cancel()
        return beats_while_waiting, result

    beats_while_waiting, result = asyncio.run(main())
    assert result.approved is True
    assert beats_while_waiting > 5, f"审批等待期间心跳应继续（实际跳了 {beats_while_waiting} 次）"


# ── AutoApprove：调试后门要留痕 ────────────────────────────
def test_auto_approve_is_marked_unsafe_and_warns():
    """★ 调试后门必须自报家门。

    它存在的最大风险是"临时改一下忘了改回去"。所以它要做两件事：
    1. unsafe=True —— RunRecorder 据此把 run 标成 unsafe_auto_approved
    2. 每次批准都打 WARNING —— 日志里可检索
    """
    ap = AutoApproveApprover()
    assert ap.unsafe is True
    assert AutoDenyApprover().unsafe is False

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        r = asyncio.run(ap.request(_req()))
    assert r.approved and r.approved_by == "auto-approve(UNSAFE)"
    assert any("AutoApproveApprover" in str(w.message) for w in caught), "必须打警告"


def test_only_real_channels_declare_themselves_safe():
    """对照实验：除 AutoApprove 外，没有别的通道自称安全。"""
    for cls in (WebApprover, CliApprover, FileApprover, AutoDenyApprover):
        assert cls.unsafe is False, f"{cls.__name__} 不该被标成 unsafe"


# ── 工厂 ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,cls",
    [
        ("web", WebApprover),
        ("cli", CliApprover),
        ("file", FileApprover),
        ("deny", AutoDenyApprover),
        ("auto-approve", AutoApproveApprover),
    ],
)
def test_factory_builds_each_channel(name, cls):
    # ★ 不传 timeout_s：AutoDenyApprover 刻意不接受它（它从不等待，
    #   接受一个超时参数只会让人以为"这个通道也会超时"）。
    #   这个不对称是有意的，所以工厂必须能原样透传 kwargs，不做统一包装。
    assert isinstance(get_approver(name), cls)


def test_factory_forwards_kwargs():
    """kwargs 要真的传到通道上 —— 尤其是 timeout_s，它决定 fail-closed 的时机。"""
    assert get_approver("cli", timeout_s=7).timeout_s == 7
    assert get_approver("file", timeout_s=8, poll_interval_s=0.5).poll_interval_s == 0.5


def test_auto_deny_refuses_a_timeout_argument():
    """对照实验：证明上面那个"不传 timeout_s"是刻意的，不是我没写。

    如果哪天有人给 AutoDenyApprover 加上 timeout_s，这条会失败 ——
    那正是需要重新想一遍"自动拒绝的通道为什么需要超时"的时刻。
    """
    with pytest.raises(TypeError):
        get_approver("deny", timeout_s=1)


def test_factory_rejects_unknown_name():
    """★ 拼错的通道名必须报错，不能悄悄退回默认通道。

    静默退回的话，"我明明配了 file 通道"会变成"实际在用 web 通道"，
    而 web 通道在无头环境里只会一直等到超时 —— 排查方向完全跑偏。
    """
    with pytest.raises(ValueError) as ei:
        get_approver("flie")  # 拼错了
    assert "flie" in str(ei.value)
    assert "file" in str(ei.value), "要列出可选项"


def test_all_implementations_satisfy_the_protocol():
    """协议是运行时可检查的 —— 拿它当一次契约回归测试。"""
    from ecom_agent.guardrails.approver import Approver

    for ap in (WebApprover(timeout_s=1), CliApprover(timeout_s=1), FileApprover(timeout_s=1),
               AutoDenyApprover(), AutoApproveApprover()):
        assert isinstance(ap, Approver), f"{type(ap).__name__} 不满足 Approver 协议"
