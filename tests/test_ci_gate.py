"""CI 门禁的真值表 —— 含**两条必做的对照实验**（2026-09-17 拍板时点名要求的）。

★★ 为什么这个文件必须存在，而不是"推到 CI 上看看颜色"：

    "什么时候算环境不可用"是一条**有真假**的判断逻辑。如果它只活在 YAML 里，
    验证它的唯一办法就是往 main 推一次真的坏提交，然后看 GitHub 上那个点是
    绿的还是红的 —— 那种验证有三个致命缺点：
      1. 不可复现（下一个人没法在本地重跑）；
      2. 要一整轮 CI（5 分钟 + 一次推送）；
      3. **它验证不了"反面"**：你没法在 CI 上"故意让环境坏掉"，
         于是"环境不可用 → 只 warning"这一半永远没被验过。
    而这一半恰恰是最危险的 —— 它是**唯一能把红洗成绿**的路径。

★★ 两条对照实验（下面对应的两条用例），以及它们各自在防什么：

    实验一：故意让一条 needs_browser 失败 → 门槛必须**真红**。
    实验二：人为让探针报"环境坏了"   → 门槛必须**只 warning**（绿）。

    ★ 只有实验一的话，一个"永远 exit 0"的实现也能通过 —— 而它会静默地
      把每一次真失败都放行。只有实验二的话，一个"永远 exit 1"的实现能通过，
      于是这个 job 又回到了"老是红、没人看"的老状态。
      两条一起才说明这个门槛**真的在分辨**，而不是固定倒向某一边。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "devtools"))

import ci_gate  # noqa: E402


def _verdict(tmp_path: Path, usable: bool, reason: str = "测试造的") -> Path:
    p = tmp_path / "verdict.json"
    p.write_text(
        json.dumps({"verdict_version": 1, "usable": usable, "reason": reason}),
        encoding="utf-8",
    )
    return p


# ── 实验一：环境可用 + 测试红 → 真红 ────────────────────────
@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_control_1_env_ok_but_tests_failed_is_a_real_red(tmp_path, outcome):
    """★★★ 对照实验一：故意让一条 needs_browser 失败，门槛必须**真红**。

    `cancelled` 一起测：它也是一种"非绿"，而它很容易在 `in ("success",)`
    这种写法下被漏掉 —— 漏掉的后果是"被取消的 run"看起来像通过。
    """
    code, msg = ci_gate.gate(
        tests_outcome=outcome, verdict_path=_verdict(tmp_path, usable=True)
    )
    assert code == ci_gate.EXIT_TESTS_FAILED, (
        f"环境可用而测试 {outcome}，门槛却放行了（退出码 {code}）—— "
        f"那这个 job 就又回到了『永远不会红』。msg={msg}"
    )
    assert "真的红" in msg


# ── 实验二：环境不可用 + 测试红 → 只 warning ────────────────
def test_control_2_env_broken_is_only_a_warning(tmp_path):
    """★★★ 对照实验二：人为让探针报环境坏，门槛必须**只 warning**（绿）。

    ★ 这条断言的是"不阻塞"，不是"看不见" —— 放行的那条路径也必须留一条
      注解（`gate` 返回的文案里带着原因）。一个安静地放行的门槛，会让
      "runner 环境在变"这件值得知道的事变成没人知道的事。
    """
    code, msg = ci_gate.gate(
        tests_outcome="failure",
        verdict_path=_verdict(tmp_path, usable=False, reason="两条路都起不来"),
    )
    assert code == ci_gate.EXIT_OK, (
        f"探针说了环境不可用，门槛却仍然红（退出码 {code}）—— "
        f"那这个 job 会因为 CI 机器的问题长期变红，然后被所有人忽略。"
    )
    assert "两条路都起不来" in msg
    assert "放行" in msg and "不是" in msg, (
        "放行时必须留字说明『这是放行、不是没红过』—— "
        f"否则看的人会以为测试真的过了。msg={msg}"
    )


# ── fail-closed：拿不准时按"真红"处理 ──────────────────────
def test_missing_verdict_file_is_treated_as_usable(tmp_path):
    """★ 探针崩了 / 没写结论 → 按**环境可用**处理（于是真红）。

    这是整个脚本里最要紧的一个默认值。反过来（默认不可用）的后果是：
    探针出任何问题都会把真实的测试失败洗成绿 ——
    一个能自动把红洗成绿的机制，比没有这个机制危险得多。
    """
    code, msg = ci_gate.gate(
        tests_outcome="failure", verdict_path=tmp_path / "根本没有这个文件.json"
    )
    assert code == ci_gate.EXIT_TESTS_FAILED, (
        "结论文件缺失时门槛放行了 —— 那『探针崩了』就等于『测试全过』。"
    )
    assert "按环境可用处理" in msg


@pytest.mark.parametrize(
    "raw",
    [
        "这不是 JSON",
        "{}",                       # 没有 usable 字段
        '{"usable": "false"}',      # ★ 字符串 "false"：truthy，必须按可用处理
        '{"usable": null}',         # null 也是 truthy 陷阱
        "[]",                       # 不是 dict
    ],
)
def test_unreadable_or_odd_verdict_files_all_fail_closed(tmp_path, raw):
    """★ 结论文件的"说不清"形态一律按可用处理（= 真红）。

    ⚠️ 其中 `{"usable": "false"}` 这条最值得单列：字符串 "false" 是 truthy，
       所以一个 `usable = data.get("usable", True)` 的实现会**判它是可用**，
       而一个 `usable = not data["usable"]` 的实现会判它**不可用**（洗绿）。
       两个都错，方向还相反。这里只认**严格的布尔 False**，
       理由是"哪个方向算坏消息"不能靠 truthiness 去猜。
    """
    p = tmp_path / "v.json"
    p.write_text(raw, encoding="utf-8")
    code, _ = ci_gate.gate(tests_outcome="failure", verdict_path=p)
    assert code == ci_gate.EXIT_TESTS_FAILED, (
        f"结论文件内容为 {raw!r} 时门槛放行了 —— 说不清的结论必须按坏消息处理"
    )


def test_strict_false_is_the_only_thing_that_marks_the_env_broken(tmp_path):
    """★ 反向锚点：只有严格的 `False` 才能把门槛放绿。

    没有这一条的话，上面那批"一律真红"的用例可以被一个
    **永远真红**的实现全部通过 —— 而那正是这个脚本要修的病。
    """
    code, _ = ci_gate.gate(
        tests_outcome="failure", verdict_path=_verdict(tmp_path, usable=False)
    )
    assert code == ci_gate.EXIT_OK


# ── 绿就是绿：测试过了不看环境 ─────────────────────────────
@pytest.mark.parametrize("outcome", ["success", "skipped"])
def test_green_tests_are_green_regardless_of_the_environment(tmp_path, outcome):
    """测试绿 → 绿，即使探针说环境坏了。

    ★ 为什么这条值得写出来：环境不可用而测试能过，是一个**不该出现**的组合
      （环境不可用的话测试根本跑不起来）。真出现了说明我们对环境的判断
      比现实更悲观 —— 那时报绿仍然是安全的：没有坏事被藏起来。
      顺便，它挡住了一种改坏法：有人把"读结论"提到最前面，
      于是探测器的偶然抽风能让一次**正常通过**的 run 变红。
    """
    code, msg = ci_gate.gate(
        tests_outcome=outcome, verdict_path=_verdict(tmp_path, usable=False)
    )
    assert code == ci_gate.EXIT_OK
    assert "无需看环境" in msg


# ── 用法错误不能和"代码坏了"混成一个颜色 ───────────────────
def test_missing_required_argument_is_a_distinct_exit_code():
    """没给 `--tests-outcome` 时退出码必须是 2，不是 1。

    ★ 理由同本项目其他地方：让"我用错了"和"代码坏了"共用一个颜色，
      等于在注解区里制造一条需要解释的红 —— 而需要解释的红，
      就是人开始忽略注解的起点。
    """
    with pytest.raises(SystemExit) as ei:
        ci_gate.main([])
    assert ei.value.code == 2
    assert ci_gate.EXIT_ERROR != ci_gate.EXIT_TESTS_FAILED
