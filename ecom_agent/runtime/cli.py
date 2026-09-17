"""命令行入口。

    main.py run   tasks/books_demo.yaml --param limit=3
    main.py compile tasks/books_demo.yaml      # 只看编译产物，不碰浏览器
    main.py runs                                # 历史列表（读 sqlite）

★★ 退出码是**契约**，不是随手写的数字：

    0  跑完且结果可用（completed）
    1  跑完了但结果不可用（failed：schema 不合法 / 步数用尽 / LLM 报错）
    3  被护栏拦停（blocked）
    2  用法或配置错误（argparse 的默认值，以及我们在启动前自己抛的）

★ 为什么 blocked 要和 failed **分开**：两者的处置完全相反 ——
  failed 值得重试（换个模型、放宽 max_steps），blocked 重试只会再撞一次护栏。
  CI 里一个 `if [ $? -eq 3 ]` 就能把这条区别用起来；混成 1 的话，
  "护栏拦了"会被当成"任务写错了"去查，方向从一开始就是错的。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from ecom_agent.config import CHROME_PATH, DB_PATH, HEADLESS, RUNS_DIR, TASKS_DIR
from ecom_agent.dsl.compiler import CompiledTask, ParamError, compile_task
from ecom_agent.dsl.loader import TaskLoadError, load_task
from ecom_agent.observability.models import BLOCKED, COMPLETED, FAILED
from ecom_agent.runtime.runner import PreflightError, RunOutcome, run_task

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3


# ── 参数解析 ──────────────────────────────────────────────
def _parse_params(pairs: list[str]) -> dict[str, str]:
    """`--param limit=3` → `{"limit": "3"}`。

    ★ 值一律保持**字符串**，类型转换交给编译器的 `_coerce_params`。
      在这里提前 int() 的话，参数的类型知识就分裂成两份（CLI 一份、DSL 一份），
      而两份必然漂移 —— 到时"CLI 说 limit 要是整数"和"YAML 说 limit 是 integer"
      会给出不同结论，且都认为自己是对的。
    ★ 缺 `=` 直接报错而不是猜测：`--param limit` 是想表达什么？
      猜"取默认值"或"设为空串"都会造出一个用户没要求的运行。
    """
    out: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise ValueError(f"--param 应该写成 名字=值，收到 {raw!r}")
        name, _, value = raw.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"--param 的名字是空的：{raw!r}")
        out[name] = value
    return out


def _resolve_approver(kind: str | None) -> tuple[Any, str]:
    """决定用哪个审批通道，并说明理由。返回 `(通道, 说明)`。

    ★ 默认值的选法值得说清楚，因为它和计划里"默认 WebApprover"不一致：
      计划里的"默认 web"是**产品层**（Phase 5 的 Web UI，那里确实该用 web）的决定。
      而 CLI 跑在 Web 层还不存在的时候 —— 默认 web 的行为是
      "往 pending/ 写一个文件，然后等 300 秒超时 → 按拒绝处理"。
      也就是**默认配置下什么都批不了，而且每次都要等满 5 分钟**。

      所以这里按"能不能交互"来选：
        · stdin 是终端 → `cli`：当场就能批，最省事；
        · 不是终端（CI、重定向、后台） → `file`：写 pending/*.json，
          由另一个进程或人回答。超时 → 拒绝（fail-closed）。
      Phase 5 的 Web 层会自己显式传 `web`，不经过这里。
    """
    from ecom_agent.guardrails.approver import get_approver

    if kind:
        return get_approver(kind), f"由 --approver 指定：{kind}"

    import os

    env_kind = os.getenv("ECOM_AGENT_APPROVER", "").strip()
    if env_kind:
        return get_approver(env_kind), f"由环境变量 ECOM_AGENT_APPROVER 指定：{env_kind}"

    interactive = sys.stdin is not None and sys.stdin.isatty()
    chosen = "cli" if interactive else "file"
    why = "stdin 是终端" if interactive else "stdin 不是终端（CI / 重定向 / 后台）"
    return get_approver(chosen), f"未指定，按可交互性选择：{chosen}（{why}）"


# ── 子命令 ────────────────────────────────────────────────
def cmd_compile(args: argparse.Namespace) -> int:
    """只编译，不运行。★ 这是"合法性检查不烧 token"的最直接体现。"""
    try:
        spec = load_task(args.task)
        compiled = compile_task(
            spec,
            _parse_params(args.param),
            chrome_path=args.chrome_path,
            headless=args.headless,
        )
    except (TaskLoadError, ParamError, ValueError) as exc:
        print(f"编译失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    print(compiled.describe())
    if args.show_text:
        print("\n── 完整任务文本（逐字节）──")
        print(compiled.task_text)
    return EXIT_OK


async def cmd_run_async(args: argparse.Namespace) -> int:
    try:
        spec = load_task(args.task)
        compiled = compile_task(
            spec,
            _parse_params(args.param),
            chrome_path=args.chrome_path,
            headless=args.headless,
        )
    except (TaskLoadError, ParamError, ValueError) as exc:
        print(f"编译失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        approver, why = _resolve_approver(args.approver)
    except ValueError as exc:
        # 只可能是通道名写错（--approver 有 choices 挡着，所以这条实际是给
        # ECOM_AGENT_APPROVER 环境变量准备的）。当场报错而不是回退到默认值 ——
        # 回退会造出一个"用户以为在用 file 通道、实际在等 web 通道超时"的运行。
        print(f"审批通道配置错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    if getattr(approver, "unsafe", False):
        print("⚠️  使用自动放行通道 —— 仅限本地调试，真实账号上不要用", file=sys.stderr)
    print(f"审批通道：{why}")

    print(compiled.describe() if args.verbose_text else
          f"任务 {compiled.task_id}（{spec.name}）指纹 {compiled.fingerprint[:16]}")

    if args.dry_run:
        print("--dry-run：已编译，未启动浏览器")
        return EXIT_OK

    try:
        outcome = await run_task(
            compiled,
            approver=approver,
            runs_dir=args.runs_dir,
        )
    except PreflightError as exc:
        # ★ 只接 PreflightError，不接 RuntimeError。
        #   "配置错了"（缺 key / start_url 不在白名单）和"跑挂了"要分开：
        #   前者该改配置，后者该看日志和栈 —— 用一条宽 except 全归成前者的话，
        #   排查方向从一开始就是错的（见 runner.PreflightError 的说明）。
        print(f"无法开始运行：{exc}", file=sys.stderr)
        return EXIT_USAGE
    _print_outcome(outcome)
    return _exit_code(outcome)


def cmd_runs(args: argparse.Namespace) -> int:
    """列历史。★ 走 sqlite 而不是扫 runs/ 目录 ——
    目录里可能有跑了但没入库的 run（落库失败），那种 run 在库里是**缺席**的，
    而缺席本身是个信号，不该被这里悄悄补上。"""
    from ecom_agent.store.repository import Repository

    if not Path(DB_PATH).exists():
        print(f"还没有数据库：{DB_PATH}（先跑一次 run）")
        return EXIT_OK

    with Repository(DB_PATH) as repo:
        rows = repo.list_runs(limit=args.limit)
    if not rows:
        print("库里还没有 run 记录")
        return EXIT_OK

    print(f"{'run_id':<30} {'任务':<24} {'状态':<10} {'解析':<15} {'行':>4}  开始时间")
    for r in rows:
        print(
            f"{r['run_id']:<30} {r['task_id']:<24} {r['status']:<10} "
            f"{r['parse_status']:<15} {r['rows_collected']:>4}  {r['started_at']}"
        )
    return EXIT_OK


def cmd_tasks(args: argparse.Namespace) -> int:
    """列任务模板。★ 读的是 TASKS_DIR 而不是写死的清单 ——
    新加一个 YAML 就自动出现在这里，不需要记得同步第二处。"""
    paths = sorted(Path(TASKS_DIR).glob("*.yaml"))
    if not paths:
        print(f"{TASKS_DIR} 下没有 .yaml")
        return EXIT_OK
    for p in paths:
        try:
            spec = load_task(p)
            print(f"{p.name:<28} {spec.id:<28} {spec.name}")
        except Exception as exc:  # noqa: BLE001 —— 一个有问题的模板不该让列表整个失败
            # ★ 但必须显式标出来：列表里静默少一个模板，
            #   看起来和"我还没写那个模板"一模一样。
            print(f"{p.name:<28} ⚠️ 加载失败：{exc}")
    return EXIT_OK


# ── 输出 ──────────────────────────────────────────────────
def _print_outcome(outcome: RunOutcome) -> None:
    print()
    print("─" * 60)
    print(f"run_id      : {outcome.run_id}")
    print(f"状态        : {outcome.status} / {outcome.parse_status}")
    print(f"采集行数    : {outcome.rows_collected}")
    print(f"步数        : {outcome.record.steps}")
    print(f"{outcome.record.llm.summary()}")
    print(f"耗时        : {outcome.record.duration_s:.1f}s")
    print(f"产物目录    : {outcome.run_dir}")
    for name, rel in sorted(outcome.record.artifacts.items()):
        print(f"    {name:<14} {rel}")

    if outcome.record.errors:
        print("错误：")
        for e in outcome.record.errors:
            print(f"    - {e}")

    # ★ 观测自身的健康度一并打出来。它们是"这份记录可不可信"的判断依据，
    #   而"可不可信"必须在看数据**之前**回答。
    rec = outcome.record
    if rec.empty_selector_map_steps:
        print(
            f"⚠️  {len(rec.empty_selector_map_steps)} 步的元素索引为空 "
            f"（护栏在这些步上一条规则都匹配不上）：{rec.empty_selector_map_steps}"
        )
    if rec.snapshot_missing_steps:
        print(f"⚠️  {len(rec.snapshot_missing_steps)} 步没有快照：{rec.snapshot_missing_steps}")
    if rec.snapshot_overwrites:
        print(f"⚠️  快照被覆盖 {rec.snapshot_overwrites} 次 —— 记录可能整体错位一帧")
    print(f"报告        : {outcome.run_dir / 'report.html'}")


def _exit_code(outcome: RunOutcome) -> int:
    if outcome.status == BLOCKED:
        return EXIT_BLOCKED
    if outcome.status == COMPLETED:
        return EXIT_OK
    if outcome.status == FAILED:
        return EXIT_FAILED
    # ★ 未知状态**不**当成成功：将来加一个新状态时，它会落到这里并被看见，
    #   而不是被 `== COMPLETED` 的反面静默当成失败或成功。
    print(f"未知状态 {outcome.status!r}，按失败处理", file=sys.stderr)
    return EXIT_FAILED


# ── CLI 组装 ──────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecom-agent",
        description="电商卖家后台自动化助手（基于 browser-use 二次开发）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 级日志")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("task", help="任务模板路径（tasks/*.yaml）")
        p.add_argument(
            "--param", action="append", default=[], metavar="名字=值",
            help="覆盖任务参数，可重复：--param limit=3",
        )
        p.add_argument(
            "--chrome-path", default=CHROME_PATH,
            help="Chrome 可执行文件。默认取 ECOM_AGENT_CHROME_PATH（config.py "
                 "里那段 CI 教训解释了为什么它必须能被显式钉死）；"
                 "显式传空串 = 让 browser-use 自己探测",
        )
        p.add_argument(
            "--no-headless", dest="headless", action="store_false", default=HEADLESS,
            help="显示浏览器窗口（排查问题时有用）",
        )

    p_run = sub.add_parser("run", help="编译并运行一个任务")
    add_common(p_run)
    p_run.add_argument(
        "--approver", default=None,
        choices=["cli", "file", "web", "deny", "auto-approve"],
        help="人工审批通道。不传则按可交互性自动选（见 --help 的说明与 cli.py）",
    )
    p_run.add_argument("--runs-dir", default=None, help=f"产物根目录（默认 {RUNS_DIR}）")
    p_run.add_argument("--dry-run", action="store_true", help="只编译，不启动浏览器")
    p_run.add_argument("--verbose-text", action="store_true", help="打印完整编译产物")

    p_compile = sub.add_parser("compile", help="只编译（不烧 token、不碰浏览器）")
    add_common(p_compile)
    p_compile.add_argument("--show-text", action="store_true", help="打印完整任务文本")

    p_runs = sub.add_parser("runs", help="列出历史 run（读 sqlite）")
    p_runs.add_argument("--limit", type=int, default=20)

    sub.add_parser("tasks", help="列出任务模板")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # ★ 把库自己的日志压到 WARNING：它在 INFO 级会打出每一步的完整提示词与
    #   DOM 文本（几千行），把我们的进度信息淹没掉 —— 而排查时想看的恰恰是
    #   我们那几行（判定了什么、拦截了什么）。要看库的细节就 -v。
    if not args.verbose:
        for noisy in ("browser_use", "BrowserSession", "Agent", "tools", "service"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        if args.command == "run":
            return asyncio.run(cmd_run_async(args))
        if args.command == "compile":
            return cmd_compile(args)
        if args.command == "runs":
            return cmd_runs(args)
        if args.command == "tasks":
            return cmd_tasks(args)
    except KeyboardInterrupt:
        print("\n已被用户中断", file=sys.stderr)
        return 130
    return EXIT_USAGE
