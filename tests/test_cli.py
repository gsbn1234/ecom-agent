"""CLI 的 `run` 路径（全离线：一条都不开浏览器、不烧 token）。

★ 为什么值得单独一个文件：

  Web 层那句"登录态不对就先说一句"有两组测试（含对照），
  而 **CLI —— 真站点首跑实际用的那条通道 —— 一条都没有**。
  同一种缺口上一轮刚以**相反的方向**出现过（CLI 正常、Web 层从没传 user_data_dir）。
  一个机制有两扇门就得两扇都测；只测一扇，等于默认另一扇是对的。

★★ 本文件每条用例都装了**绊线**：`run_task` 被换成"一被调用就炸"。
  于是它们顺带证明了另一件事 —— `--dry-run` 确实在启动任何东西**之前**就返回了。
  没有这根线，"dry-run 不碰浏览器"就只是注释里的承诺。

★★★ 这里守的还是那个家族的失败形态：**最可能让真站点 run 静默零行的
  那件事（登录态），恰恰是彩排不会告诉你的那一件。** `check_profile`
  不碰浏览器（只读盘上的标记文件），所以它在 `--dry-run` 早退之前是零成本的。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ecom_agent.config import TASKS_DIR
from ecom_agent.runtime import cli
from ecom_agent.runtime.profile import write_mark

NEEDS_LOGIN = TASKS_DIR / "pdd_shop_overview.yaml"
NO_LOGIN = TASKS_DIR / "mock_shop_readonly.yaml"
VERIFIED_AT = "2026-09-17T11:49:06+00:00"


def _ready_profile(tmp_path: Path) -> Path:
    """造一个**复核过**的 profile（就是 login_pdd.py 成功时会留下的那种）。"""
    profile = tmp_path / "profile"
    write_mark(
        profile,
        site="mms.pinduoduo.com",
        logged_in_url="https://mms.pinduoduo.com/home/",
        verified_at=VERIFIED_AT,
    )
    return profile


def _run_dry(monkeypatch, task: Path, *, profile: str) -> int:
    """跑一次 `--dry-run`，返回退出码。★ 全程不会启动浏览器、不会烧 token。"""

    async def tripwire(*_a, **_kw):
        raise AssertionError(
            "`--dry-run` 竟然走到了 run_task —— dry-run 的契约是"
            "「编译完就返回，不启动任何东西」，这里意味着它被破坏了"
        )

    monkeypatch.setattr(cli, "run_task", tripwire)
    args = cli.build_parser().parse_args(
        ["run", str(task), "--dry-run", "--approver", "deny", "--profile", profile]
    )
    return asyncio.run(cli.cmd_run_async(args))


def test_dry_run_reports_the_login_state_before_it_returns(monkeypatch, tmp_path, capsys):
    """★★ 承重的一条：**彩排必须能看见登录态**。

    这条断言的位置就是它的全部意义。检查曾经落在 `--dry-run` 早退**之后**，
    于是彩排打出的只有"已编译，未启动浏览器"—— 恰好看不见那件最可能
    让正式跑**零行数据**的事，而零行数据的样子是"退出码 0、报告齐全"，
    人会先去怀疑风控。

    ★ 对照在同一个用例里取：同一条命令、只把 profile 换成一个没登录过的目录，
      就必须**不再**报"就绪"（见下一条用例）。没有那个方向的话，
      一个"无条件打印登录态就绪"的实现也能让这里通过。
    """
    code = _run_dry(monkeypatch, NEEDS_LOGIN, profile=str(_ready_profile(tmp_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert "登录态：" in captured.out, (
        f"彩排没有报出登录态 —— 它被挡在 `--dry-run` 早退之后了。\n"
        f"stdout 只有：{captured.out!r}"
    )
    assert "mms.pinduoduo.com" in captured.out, "回执要说清用的是**哪个**登录态"
    assert "fresh_session" in captured.out, "回执要说清这个登录态是被复核过的"
    # ★ 另一半：dry-run 自己那行不能被挤掉 —— 检查是**移到前面**，不是把它顶掉。
    assert "已编译" in captured.out, "dry-run 自己的收尾行不见了"


def test_dry_run_on_an_unusable_profile_says_so_and_stays_exit_zero(
    monkeypatch, tmp_path, capsys
):
    """★★ 彩排里最该出现的就是这句：登录态不对时**必须说话**，且要说清怎么修。

    ★ 退出码仍然是 0 —— 它是**提示不是门禁**。cookie 会过期，而"标记存在"
      只说明那一刻复核过；拿它拦住运行会在最不该拦的时候（刚过期、急着看报告）拦人。
      所以这里断言的是"说了话"，不是"拦住了"。
    """
    code = _run_dry(monkeypatch, NEEDS_LOGIN, profile="")
    captured = capsys.readouterr()

    assert code == 0, "登录态不对是提示不是门禁 —— 它不该改变退出码"
    assert "登录态检查" in captured.err, f"没配 profile 却一声不响：{captured.err!r}"
    assert "ECOM_AGENT_USER_DATA_DIR=" in captured.err, (
        "警告里要含**可照抄的下一步**，否则它只制造焦虑"
    )
    # ★ 对照：这时 stdout 里**不该**出现那句放行回执。
    assert "登录态：" not in captured.out, "登录态不 OK 却打了就绪回执"


def test_a_template_that_does_not_need_login_stays_quiet(monkeypatch, tmp_path, capsys):
    """★ 不需要登录的模板**一个字都不该提登录** —— 哪怕 profile 是坏的。

    这是代码里那条"只在 requires_login 的任务上做这个检查"的判据。
    它值得钉住：每次都唠叨一遍的警告会退化成没人看的背景噪声，
    而噪声一旦成为常态，上面那条**真该被看见**的警告也就一起被忽略了。
    """
    code = _run_dry(monkeypatch, NO_LOGIN, profile="")
    captured = capsys.readouterr()

    assert code == 0
    assert "登录态" not in captured.out
    assert "登录态" not in captured.err, (
        f"这个模板根本不登录，却报了登录态 —— 警告一旦变成噪声就没人看了：{captured.err!r}"
    )
