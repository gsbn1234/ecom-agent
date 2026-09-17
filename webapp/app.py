"""Web 层：实时看板 + 网页审批（Phase 5）。

★ 为什么是 FastAPI + 原生单页，而不是 Streamlit（ADR-12）：
  Streamlit 的 rerun 模型（任何交互都重跑整个脚本）与「长驻的审批按钮 + SSE
  实时流」是直接冲突的 —— 而这恰恰是本 UI 的核心交互。原生单页零依赖零构建，
  并且**同一份代码加个 `?run_id=` 就变成历史回放视图**（因为回放和实时推的是
  同形状的事件，见 observability/events.py）。

★★ 本文件的三个职责，边界要清楚：

  1. **装配**：把 DSL 模板 → 编译 → runner → 事件总线 → SSE 串起来。
     它**不实现**任何护栏/编排逻辑 —— 那些在 ecom_agent 里，CLI 走的是同一份。
     Web 层不是"另一套 runner"，是一个新的**消费者**。
  2. **会话状态**：谁在等审批、哪个 run 还活着。这份状态只活在进程内
     （见 `RunRegistry` 的说明：它刻意不落盘）。
  3. **传输**：把事件总线上的东西按 SSE 帧推出去。

★ 审批为什么能"点一下就把 run 唤醒"：WebApprover 等的是一个 `asyncio.Event`，
  而 FastAPI 的 handler 和 run 任务**在同一个事件循环里**，所以 POST 决策
  是真的唤醒，不是轮询文件。这也是 WebApprover 是默认通道的理由。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ecom_agent.config import (
    APPROVAL_TIMEOUT_S,
    CHROME_PATH,
    DB_PATH,
    LIVE_LLM,
    RUNS_DIR,
    TASKS_DIR,
)
from ecom_agent.dsl.compiler import CompiledTask, ParamError, compile_task
from ecom_agent.dsl.loader import TaskLoadError, load_task
from ecom_agent.guardrails.approver import WebApprover
from ecom_agent.observability.events import EventBus, events_from_run_dir
from ecom_agent.runtime.runner import PreflightError, RunOutcome, run_task

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SSE_MEDIA_TYPE = "text/event-stream"

RunFunc = Callable[..., Awaitable[RunOutcome]]
"""跑一次任务的函数。**这是唯一被注入的东西**。

★ 为什么留这个注入口，而不是直接 `from ... import run_task` 就调用：
  没有它，`tests/test_api.py` 测一个端点就得起真浏览器 + 真 LLM —— 于是
  这个文件里的**所有**分支（模板解析失败、run 崩了、审批 id 不存在、
  健康检查两档）都变成"只能手工验"。
  于是它们就真的只会被手工验，而手工验不会去点那些分支。
  注入之后，API 的形状可以在毫秒级、零 token 的前提下被钉死在测试里。
"""


# ── 会话状态 ──────────────────────────────────────────────
@dataclass
class LiveRun:
    """一个**进程内**活着的 run。

    ★★ 为什么它刻意不落盘、也不打算在进程重启后恢复：
      它持有的东西（`WebApprover` 的 asyncio.Event、任务句柄）本来就是
      **进程内的** —— 落盘也恢复不了一个正在 await 的 Event。
      而 run 的**事实**已经在盘上了（steps.jsonl / run.json），
      重启之后看板读回放就行（`GET /api/runs/{id}` 走的就是那条路）。

      所以这里的诚实表述是：**这张表是缓存，不是真相。** 真相在 `runs/`。
      进程重启 → 表空了 → 进行中的 run 变成"历史里一个没有结尾的 run"。
      那不是数据损坏，那是一个准确的描述：那个 run 确实没跑完。
    """

    run_id: str
    task_id: str
    bus: EventBus
    approver: WebApprover
    status: str = "running"
    """running / completed / failed / rejected"""
    error: str = ""
    outcome: RunOutcome | None = None
    task: asyncio.Task | None = None
    """★ 必须**持有**这个句柄。asyncio 只保留任务的弱引用 ——
      不持有的话，一个还在跑的 run 可能被 GC 掉，表现是"run 无声无息地停了"，
      而且不会有任何报错。这是一个只在 GC 时机恰好时出现的 bug。"""

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "error": self.error,
            "waiting_approvals": self.approver.waiting(),
            "steps": 0 if self.outcome is None else self.outcome.record.steps,
        }


@dataclass
class RunRegistry:
    """run_id → LiveRun。★ 见 LiveRun 的说明：这是缓存，真相在 runs/。"""

    runs: dict[str, LiveRun] = field(default_factory=dict)

    def add(self, live: LiveRun) -> None:
        self.runs[live.run_id] = live

    def get(self, run_id: str) -> LiveRun | None:
        return self.runs.get(run_id)

    def require(self, run_id: str) -> LiveRun:
        live = self.runs.get(run_id)
        if live is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"没有正在跟踪的 run {run_id!r}。"
                    "如果它是上一次进程启动时跑的，它已经不在内存里了 —— "
                    f"用 GET /api/runs/{run_id} 读盘上的回放。"
                ),
            )
        return live


# ── 请求 / 响应体 ─────────────────────────────────────────
class StartRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class ApprovalBody(BaseModel):
    """审批决策。

    ★ `approved` 是**必填**且没有默认值，这是刻意的：
      `{"note": "看起来没问题"}` 这种漏了 approved 的请求，如果默认成
      approve 就是"一句话把危险动作放行了"；默认成 deny 又会让人困惑
      "我明明点了批准"。必填 → 422，意图不明时**不猜**。
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    approved_by: str = "web"
    note: str = ""


# ── 应用工厂 ──────────────────────────────────────────────
def create_app(
    *,
    runs_dir: Path | str | None = None,
    tasks_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    run_func: RunFunc | None = None,
    chrome_path: str | None = None,
    live_llm: bool | None = None,
) -> FastAPI:
    """造 app。所有外部依赖都能注入 —— 测试里没有一个真实环境依赖。"""
    runs = Path(runs_dir or RUNS_DIR)
    tasks = Path(tasks_dir or TASKS_DIR)
    db = Path(db_path or DB_PATH)
    # ★ 审批目录**跟着 runs 目录走**，不各自去取全局默认值。
    #   config.py 里这三者的关系是 `APPROVALS_DIR = RUNS_DIR / "approvals"` ——
    #   也就是"审批流水是 run 证据的一部分"。
    #   所以换了 runs_dir 就必须一起换：不换的话，一次 run 的证据会被劈成两半
    #   （steps.jsonl 在注入的目录里，审批流水落进默认目录），
    #   而默认部署下两者恰好重合 —— 于是这个错在本地**永远看不见**，
    #   只在"换个目录跑"的时候出现（测试、或任何非默认部署）。
    approvals = runs / "approvals"
    runner: RunFunc = run_func or run_task
    registry = RunRegistry()

    app = FastAPI(
        title="ecom-agent 看板",
        description="browser-use 二次开发的电商卖家后台自动化助手 —— 实时看板 + 网页审批",
        version="0.5.0",
    )
    app.state.runs = runs
    app.state.tasks = tasks
    app.state.db = db
    app.state.registry = registry

    # ── 页面 ──────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            # ★ 说得清楚一点，而不是让 FastAPI 抛一个 FileNotFoundError 的栈。
            #   单页是**运行时读盘**的（无构建步骤），所以"文件不在"是一个
            #   真实可能的部署错误，不是不可能事件。
            raise HTTPException(
                status_code=500,
                detail=f"单页文件缺失：{page}（webapp/static/index.html 未被部署）",
            )
        return HTMLResponse(page.read_text(encoding="utf-8"))

    # ── 模板 ──────────────────────────────────────────────
    @app.get("/api/templates")
    async def list_templates() -> dict[str, Any]:
        """列出模板及其参数。★ 表单是**从 TaskSpec 生成**的，不是手写的。

        手写表单的问题不是麻烦，是**会漂移**：YAML 里加了参数、前端不知道，
        于是那个参数永远取默认值。而默认值是"任务能跑起来"的，所以
        漂移的表现是"功能没生效"，不是"报错"。
        """
        out: list[dict[str, Any]] = []
        for path in sorted(tasks.glob("*.yaml")):
            try:
                spec = load_task(path)
            except TaskLoadError as exc:
                # ★ 坏模板列出来并带上原因，而不是跳过。
                #   静默跳过的话，一个语法写错的模板在界面上"不存在"，
                #   而它明明躺在 tasks/ 里 —— 人会去查前端。
                #
                # ★ 键名与好模板**保持一致**（都是 `template_id`），不另起一个
                #   （比如 `file`）。同一个概念两个键名，客户端就得写
                #   `t.template_id || t.file`，而"这两处什么时候不一致"是个
                #   没人能回答的问题。写这条接口的测试时立刻就撞上了：
                #   照着好模板的键名去断言，坏的那条读不出来。
                out.append({"template_id": path.name, "broken": str(exc)})
                continue
            out.append(
                {
                    "template_id": path.name,
                    "id": spec.id,
                    "name": spec.name,
                    "site": spec.site,
                    "requires_login": spec.requires_login,
                    "start_url": spec.start_url,
                    "goal": spec.goal,
                    "params": _param_schema(spec),
                    "guardrails": {
                        "default_decision": spec.guardrails.default_decision.value,
                        "rules": [r.id for r in spec.guardrails.rules],
                        "allowed_domains": spec.guardrails.allowed_domains,
                    },
                }
            )
        return {"templates": out}

    # ── 起一个 run ────────────────────────────────────────
    @app.post("/api/runs", status_code=202)
    async def start_run(body: StartRunBody) -> dict[str, Any]:
        """起一个 run，**立刻**返回 run_id，任务在后台跑。

        ★ 为什么不 await 到跑完再返回（那会让这个接口简单很多）：
          一次 run 的时长以分钟计，中途还会**等人点审批**（默认 300s 超时）。
          同步返回意味着 HTTP 请求挂在那儿，而浏览器/Curl/任何中间层都会
          先超时 —— 于是"审批还没点，连接已经断了"。
          看板的整个交互模型建立在"先连上 SSE 看着"之上，所以这里必须异步。
        """
        path = _resolve_template(tasks, body.template_id)
        try:
            spec = load_task(path)
        except TaskLoadError as exc:
            raise HTTPException(status_code=400, detail=f"模板加载失败：{exc}") from exc

        try:
            compiled = compile_task(spec, body.params)
        except ParamError as exc:
            # ★ 参数错误在**创建浏览器之前**就被拦下（compile_task 的顺序保证）。
            #   400 而不是 500：这是调用方的输入问题，不是服务坏了。
            raise HTTPException(status_code=400, detail=f"参数不合法：{exc}") from exc

        # ★ run_id 由这里生成并**交给** runner，而不是让 runner 自己造了再回来问。
        #   因为总线、审批通道、看板都要在 run 开始**之前**就拿着同一个 id ——
        #   否则会出现一小段"run 已经在跑，但 SSE 还不知道该订阅哪个 id"的窗口，
        #   而那段窗口里发生的事件就永远看不到了。
        from ecom_agent.observability.recorder import new_run_id

        run_id = new_run_id()
        bus = EventBus(run_id=run_id)
        approver = WebApprover(
            run_id=run_id,
            timeout_s=APPROVAL_TIMEOUT_S,
            pending_dir=approvals / "pending",
            decided_dir=approvals / "decided",
        )
        live = LiveRun(run_id=run_id, task_id=compiled.task_id, bus=bus, approver=approver)
        registry.add(live)

        live.task = asyncio.create_task(
            _drive(live, compiled, approver=approver, bus=bus, runner=runner, runs_dir=runs)
        )
        return {"run_id": run_id, "status": "running", "task_id": compiled.task_id}

    # ── SSE ───────────────────────────────────────────────
    @app.get(
        "/api/runs/{run_id}/stream",
        responses={
            200: {
                "content": {SSE_MEDIA_TYPE: {}},
                "description": (
                    "SSE 事件流。事件名见 `event:` 字段：run_started / step_completed / "
                    "guardrail_blocked / approval_required / approval_resolved / "
                    "run_completed / error / stream_gap。\n\n"
                    "★ `step_completed` 的 data **就是** steps.jsonl 里的一行，"
                    "逐字节同源 —— 所以回放视图与实时视图共用同一个渲染器。\n\n"
                    "★ run 已经不在内存里（进程重启过）时，本端点**自动降级为回放**："
                    "把盘上的事件按同样的事件名推一遍再正常结束。不是 404 —— "
                    "看板刷新页面时不该看到一个错误，它要的东西盘上全都有。"
                ),
            }
        },
    )
    async def stream(run_id: str) -> StreamingResponse:
        live = registry.get(run_id)
        if live is not None:
            source = live.bus.subscribe()
        else:
            # ★ 降级为回放。这条路径与"实时"共用同一个渲染器，
            #   因为事件名和载荷形状完全一样（见 events_from_run_dir）。
            #
            # ★★ 但"降级为回放"的前提是**盘上真有这个 run**。盘上也没有时
            #    回 404，而不是一条空流 —— 这条分支是实测补的：
            #      · 以前它直接走进回放，而 events_from_run_dir 对一个不存在的
            #        目录会产出**一条空的 run_started**，前端于是显示出一个
            #        "开始过、但什么都没有"的 run。人会去查"这次 run 为什么
            #        一步都没跑"，而真相是这个 id 根本不存在（链接过期/手输错）。
            #      · 404 也让本端点与 `GET /api/runs/{id}` 对**同一个条件**
            #        给出同一个答案 —— 两个接口不该对"这个 run 在不在"有两种说法。
            #    ⚠️ 别把这条和"进程重启过、run 只在盘上"混起来：那种情况目录
            #       是存在的，回放照常 —— 那正是本端点不 404 的理由。
            run_dir = runs / run_id
            if not run_dir.is_dir():
                raise HTTPException(status_code=404, detail=f"找不到 run {run_id!r}")
            source = _replay_stream(run_dir)
        return StreamingResponse(
            _sse_frames(source),
            media_type=SSE_MEDIA_TYPE,
            headers={
                # ★ 关掉所有缓冲。少了这个头，nginx/某些代理会把 SSE 攒成一块再发，
                #   于是"实时"看板变成"最后一次性出现" —— 看起来像是后端没推。
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── 审批决策 ──────────────────────────────────────────
    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def decide(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        live = registry.require(run_id)
        ok = live.approver.resolve(
            approval_id, body.approved, approved_by=body.approved_by, note=body.note
        )
        if not ok:
            # ★ 409 而不是 404：这条审批**曾经**存在。区分这两者很重要 ——
            #   404 会让人以为"卡片是假的/前端显示了不存在的东西"，
            #   而 409 说的是"你来晚了：它已经超时或被决策过了"，
            #   后者才是真实发生的事（超时是默认 300s，很容易发生）。
            raise HTTPException(
                status_code=409,
                detail=(
                    f"审批 {approval_id!r} 已不在等待中（已决策或已超时）。"
                    f"当前仍在等待：{live.approver.waiting()}"
                ),
            )
        return {"ok": True, "approval_id": approval_id, "approved": body.approved}

    # ── 历史 / 回放 ───────────────────────────────────────
    @app.get("/api/runs")
    async def list_runs(limit: int = 50) -> dict[str, Any]:
        return {"runs": _list_run_dirs(runs, limit=limit, registry=registry)}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """一个 run 的完整回放。

        ★ 它同时是 SSE 的**兜底**：前端收到 `stream_gap`（订阅者太慢、
          总线丢了事件）或发现 seq 跳号时，就调这个接口重新拉全量。
          两条路通向同一份数据，所以"补数据"不需要第二套逻辑。
        """
        run_dir = runs / run_id
        if not run_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"找不到 run {run_id!r}")
        events = events_from_run_dir(run_dir)
        record = _read_json(run_dir / "run.json")
        live = registry.get(run_id)
        return {
            "run_id": run_id,
            "record": record,
            "events": [e.model_dump(mode="json") for e in events],
            "live": None if live is None else live.public(),
        }

    @app.get("/api/runs/{run_id}/steps/{step}/screenshot")
    async def screenshot(run_id: str, step: int) -> FileResponse:
        """某一步的截图。

        ★ 路径来自**这一步自己的记录**（`screenshot_path`），不是拼出来的。
          拼路径在这里是错的：截图按 attempt 分了子目录（`a01/`、`a02/`），
          而"这个步号属于哪次 attempt"只有记录知道 ——
          拼的话重试过的 run 会稳定地返回第一次尝试的图。
        """
        run_dir = (runs / run_id).resolve()
        if not run_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"找不到 run {run_id!r}")
        rel = _screenshot_rel(run_dir, step)
        if not rel:
            raise HTTPException(status_code=404, detail=f"step {step} 没有截图记录")
        target = (run_dir / rel).resolve()
        # ★ 解析之后必须仍在 run 目录内。理由不是"我们的记录可能被篡改"，
        #   而是这条路径最终来自**盘上的文件内容**，而盘上的文件是可以被
        #   任何能写 runs/ 的进程改的 —— 一个 `../../.env` 就够读到密钥了。
        #   一行 startswith 换掉一整类问题，没有理由省。
        if not target.is_relative_to(run_dir):
            logger.warning("拒绝越界的截图路径：run=%s step=%s rel=%r", run_id, step, rel)
            raise HTTPException(status_code=400, detail="截图路径越界")
        if not target.is_file():
            # ★ 记录里有路径但文件不在，是一个真实状态（图不是 PNG 时**刻意不落盘**，
            #   见 recorder._persist_screenshot）。说清楚它，别报"文件不存在"。
            raise HTTPException(
                status_code=404,
                detail=(
                    f"step {step} 的记录里有截图路径 {rel!r}，但文件不在盘上。"
                    "这不是 bug：截图不是真 PNG 时 recorder 会拒绝落盘"
                    "（宁可缺一张图，也不给一张打开才发现坏掉的图）。"
                ),
            )
        return FileResponse(target, media_type="image/png")

    # ── 健康检查（两档）───────────────────────────────────
    @app.get("/api/health")
    async def health() -> JSONResponse:
        """两档健康检查。

        ★★ 为什么要分两档，而不是一个 200/503：
          硬依赖（能写盘、能建目录）坏了 → 这个服务**什么都做不了**，503。
          软依赖（浏览器路径、LLM key）坏了 → 服务本身是好的，
          **可以列历史、可以回放、可以审批**，只是起不了新 run。
          把软依赖也判成 503 会让"看历史"这个功能被误杀 ——
          而"浏览器起不来时还能回放证据"恰恰是审计场景最需要的。
          所以软依赖坏 → 200 + `degraded`，让调用方自己决定。
        """
        hard: dict[str, Any] = {}
        soft: dict[str, Any] = {}

        # 硬 1：runs 目录可建、可写
        try:
            runs.mkdir(parents=True, exist_ok=True)
            probe = runs / ".health-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            hard["runs_dir_writable"] = {"ok": True, "path": str(runs)}
        except OSError as exc:
            hard["runs_dir_writable"] = {"ok": False, "path": str(runs), "error": str(exc)}

        # 硬 2：SQLite 可写。★ 真的写一次再回滚，而不是只看文件在不在 ——
        #   "文件存在但没有写权限"是一个在只读挂载的容器里很常见的部署错误。
        try:
            db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS _health (x INTEGER)")
                conn.execute("INSERT INTO _health VALUES (1)")
                conn.rollback()
            hard["db_writable"] = {"ok": True, "path": str(db)}
        except sqlite3.Error as exc:
            hard["db_writable"] = {"ok": False, "path": str(db), "error": str(exc)}

        # 软 1：浏览器
        chrome = CHROME_PATH if chrome_path is None else chrome_path
        if chrome:
            exists = Path(chrome).is_file()
            soft["chrome"] = {"ok": exists, "path": chrome}
            if not exists:
                soft["chrome"]["hint"] = "路径不存在；空 ECOM_AGENT_CHROME_PATH 可让库自行探测"
        else:
            # ★ 空串不是"没配"，是"明确要求库自己探测"（config.py 里区分了这两种）。
            #   所以它不算降级 —— 报成 degraded 会训练人忽略这个字段。
            soft["chrome"] = {"ok": True, "path": "", "note": "未钉死，由 browser-use 自行探测"}

        # 软 2：LLM
        enabled = LIVE_LLM if live_llm is None else live_llm
        soft["llm"] = {
            "ok": bool(enabled),
            "live": bool(enabled),
            "hint": "" if enabled else "未开启真调 LLM：可看历史与回放，但起不了新 run",
        }

        hard_ok = all(v["ok"] for v in hard.values())
        soft_ok = all(v["ok"] for v in soft.values())
        if not hard_ok:
            status = "unhealthy"
        elif not soft_ok:
            status = "degraded"
        else:
            status = "ok"
        return JSONResponse(
            {"status": status, "hard": hard, "soft": soft},
            status_code=200 if hard_ok else 503,
        )

    return app


# ── 后台跑一个 run ────────────────────────────────────────
async def _drive(
    live: LiveRun,
    compiled: CompiledTask,
    *,
    approver: WebApprover,
    bus: EventBus,
    runner: RunFunc,
    runs_dir: Path,
) -> None:
    """跑 run，并把结局落进 LiveRun + 总线。

    ★★ 这里**吞掉所有异常**并转成 SSE 的 `error` 事件，这不是偷懒：
      计划里的契约是"异常转 SSE error 事件而不是断连"。
      后台任务里抛出的异常如果没人接，会变成一条 asyncio 的
      "Task exception was never retrieved" 日志 —— 而**看板那边只会看到
      连接停在那儿不动**。人看到的是"卡住了"，实际是任务已经死了。
      这两种表现必须被分开，否则排查方向完全不同。
    """
    try:
        outcome = await runner(
            compiled, approver=approver, run_id=live.run_id, events=bus, runs_dir=runs_dir
        )
        live.outcome = outcome
        live.status = "completed" if outcome.ok else "failed"
    except PreflightError as exc:
        # ★ 预检失败单独一支：它不是"run 崩了"，是"根本没开跑"。
        #   两者的处置完全不同（改配置 vs 查现场），所以不能在事件里合并。
        live.status = "rejected"
        live.error = str(exc)
        bus.publish(_error_event(live.run_id, "preflight", exc, fatal=True))
    except asyncio.CancelledError:
        live.status = "failed"
        live.error = "任务被取消"
        raise
    except Exception as exc:  # noqa: BLE001 —— 见上面 docstring
        logger.exception("run %s 异常终止", live.run_id)
        live.status = "failed"
        live.error = f"{type(exc).__name__}: {exc}"
        bus.publish(_error_event(live.run_id, "run", exc, fatal=True))
    finally:
        # ★ close() 必须在 finally 里，且必须在**所有**结局上执行：
        #   它是订阅者唯一的"不会再有新事件了"信号。漏掉的话，
        #   SSE 生成器会永远挂在 `await q.get()` 上 ——
        #   浏览器标签页一直转圈，而服务端认为一切正常。
        bus.close()


def _error_event(run_id: str, where: str, exc: Exception, *, fatal: bool) -> Any:
    from ecom_agent.observability.events import RunEvent

    return RunEvent(
        type="error",
        run_id=run_id,
        data={
            "where": where,
            "kind": type(exc).__name__,
            "message": str(exc),
            "fatal": fatal,
        },
    )


# ── 传输层小工具 ──────────────────────────────────────────
async def _sse_frames(source: Any) -> Any:
    """把事件流变成 SSE 帧。

    ★ 这里是**唯一**做编码的地方，实时和回放共用它 —— 因为
      两个来源产出的都是 `RunEvent`，`sse()` 是同一个方法。
      所以"回放视图不是另做一套渲染"这条约束在传输层也是成立的，
      不只是在数据层。

    ★★ `aclosing` 不是装饰，是这段代码正确性的一半：客户端断连时
      任务被取消，取消点在 `yield` 上 —— 也就是在 `async with` 里面，
      于是上游生成器会被**当场**关掉，`bus.subscribe()` 的 finally
      （把订阅者从集合里摘掉）随之跑掉。

      不写 `aclosing` 会怎样：`async for` 被取消时**不会**关闭上游
      async generator，那个 finally 要等垃圾回收或事件循环的 asyncgen
      终结钩子才跑。在这段窗口里，总线仍然认为"有人在看"，
      会继续往一个没人在读的队列里投递 —— 队列填满后开始计入 `dropped`。
      而 `dropped` 是**要显示给人看的**数字（"已丢弃 N 条"），
      于是一个早就关掉的标签页能让正常观众看到一个虚高的丢弃计数：
      看板上出现一句我们自己的代码编出来的假警报。
      （时限很短、量也有限，但"计数器说的不是真事"没有可接受的量级。）
    """
    async with contextlib.aclosing(source):
        async for event in source:
            yield event.sse()


async def _replay_stream(run_dir: Path) -> Any:
    """把盘上的事件当流推出去，推完就结束。"""
    for event in events_from_run_dir(run_dir):
        yield event
        # ★ 让出一次事件循环。不加的话，一个几千步的 run 会在一个 tick 里
        #   把全部帧灌进发送缓冲区 —— 前端会先卡住再一次性全出现，
        #   恰好毁掉"实时看板"和"回放"看起来一样这件事。
        await asyncio.sleep(0)


# ── 纯函数小工具 ──────────────────────────────────────────
def _resolve_template(tasks: Path, template_id: str) -> Path:
    """把 template_id 变成一个**在 tasks/ 里面**的路径。

    ★ `template_id` 直接来自 HTTP 请求体，所以这里必须挡住 `../`：
      `{"template_id": "../../.env"}` 会被 load_task 当 YAML 读，
      而 PyYAML 读不了就报错 —— 报错信息里会**带上文件内容片段**，
      于是密钥从错误响应里漏出去。
      这不是假想：这条路径短、看着人畜无害，正是容易被漏掉的那类。
    """
    name = Path(template_id).name
    if not name or name != template_id or name.startswith("."):
        raise HTTPException(status_code=400, detail=f"非法的 template_id：{template_id!r}")
    path = (tasks / name).resolve()
    if not path.is_relative_to(tasks.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail=f"找不到模板 {template_id!r}")
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise HTTPException(status_code=400, detail=f"模板必须是 YAML：{template_id!r}")
    return path


def _param_schema(spec: Any) -> list[dict[str, Any]]:
    """TaskSpec.params → 一份前端能直接画表单的描述。

    ★ 用一份**自己的**小 schema 而不是 pydantic 的 `model_json_schema()`：
      后者会把 `ParamSpec` 这个**实现类型**的形状（`ge`/`le`/`enum` 的嵌套、
      `$defs` 引用…）泄漏成前端契约，于是"改一下 ParamSpec 的内部结构"
      就变成一次前端破坏性变更。这里显式投影，改内部不动前端。
    """
    out: list[dict[str, Any]] = []
    for name, ps in (spec.params or {}).items():
        out.append(
            {
                "name": name,
                "type": ps.type,
                "required": ps.required,
                "default": ps.default,
                "description": ps.description,
                "enum": list(ps.enum) if ps.enum else None,
                "minimum": ps.ge,
                "maximum": ps.le,
            }
        )
    return out


def _read_json(path: Path) -> dict[str, Any]:
    import json

    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # ★ 读不动就返回空，但**不吞掉**：损坏的 run.json 是一个值得知道的事实。
        logger.warning("run.json 解析失败（文件可能被截断）：%s", path)
        return {}


def _list_run_dirs(runs: Path, *, limit: int, registry: RunRegistry) -> list[dict[str, Any]]:
    """列出 runs/ 下的历史。★ 以**盘**为准，内存里的活 run 只是补充标记。"""
    if not runs.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for d in runs.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name == "approvals":
            # ★ 审批流水目录不是 run，别把它列成一个 run。
            continue
        record = _read_json(d / "run.json")
        live = registry.get(d.name)
        items.append(
            {
                "run_id": d.name,
                # ★ 没有 run.json 的目录也列出来、并标出来。
                #   那种目录的含义是"run 起了但还没来得及写 run.json 就死了"——
                #   它是一个**有诊断价值的事实**，跳过它等于把它藏起来。
                "incomplete": not record,
                "task_id": record.get("task_id", ""),
                "task_name": record.get("task_name", ""),
                "status": record.get("status", ""),
                "parse_status": record.get("parse_status", ""),
                "rows_collected": record.get("rows_collected", 0),
                "steps": record.get("steps", 0),
                "started_at": record.get("started_at", ""),
                "finished_at": record.get("finished_at", ""),
                "duration_s": record.get("duration_s", 0.0),
                "unsafe_auto_approved": record.get("unsafe_auto_approved", False),
                "live_status": None if live is None else live.status,
            }
        )
    # ★ 按 run_id 倒序：它是 `20260917T050218+0000-4ec444` 这种形态，
    #   字符串序即时间序（new_run_id 的实现保证），所以不需要解析时间。
    items.sort(key=lambda x: x["run_id"], reverse=True)
    return items[:limit]


def _screenshot_rel(run_dir: Path, step: int) -> str:
    """从 steps.jsonl 里找出这一步的截图相对路径。找不到返回空串。"""
    import json

    steps = run_dir / "steps.jsonl"
    if not steps.is_file():
        return ""
    for line in steps.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if int(rec.get("step", -1)) == step:
            return str(rec.get("screenshot_path") or "")
    return ""


# ★ 模块级的 app 实例，给 `uvicorn webapp.app:app` 用。
#   注意它用的是**默认**依赖（真 runs/、真 tasks/、真 run_task）——
#   测试一律走 create_app(...)，不碰这个。
app = create_app()
