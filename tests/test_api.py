"""Web 层的契约 —— 端点形状、状态码语义、SSE 传输层。

★★ 这个文件里**没有一个真实依赖**：不起浏览器、不调 LLM、不连网络。
  靠的是 `create_app(run_func=...)` 那一个注入口 —— 见 app.py 里 `RunFunc`
  的 docstring：没有它，这个文件里的每条分支（模板坏了、run 崩了、
  审批 id 不存在、健康检查两档）都会变成"只能手工验"，而手工验不会去点它们。

★★ 这个文件里最值钱的不是"200 回来了"，而是**状态码之间的区分**：
    · 400（你的输入不对）vs 404（这东西不存在）vs 409（你来晚了）
    · 硬依赖坏 → 503 vs 软依赖坏 → 200 + degraded
    · 参数不合法 → 400，而且**浏览器一次都没起**
  这些区分在代码里各占一行，看起来都像琐碎选择；但把 409 写成 404，
  人就会去查"审批卡片是不是假的"，而真正的事是"它超时了"。
  所以每条都值得钉住 —— 钉的不是返回值，是**它想传达的那件事**。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from ecom_agent.guardrails.approver import ApprovalOutcome, ApprovalRequest, WebApprover
from ecom_agent.observability.events import RunEvent
from ecom_agent.observability.models import COMPLETED, RunRecord
from ecom_agent.runtime.profile import write_mark
from ecom_agent.runtime.runner import PreflightError, RunOutcome
from webapp.app import create_app

# ── 夹具：一份最小可用模板 + 一个坏模板 ────────────────────
GOOD_TEMPLATE = """\
schema_version: "1"
id: test.api_demo
name: 测试用模板
site: demo
requires_login: false
start_url: "https://books.toscrape.com/"
goal: >-
  采集 {limit} 条，只读。
steps:
  - "打开页面。"
  - "读取列表。"
  - "调用 done 返回。"
params:
  limit:
    type: integer
    default: 3
    ge: 1
    le: 10
    description: "最多提取多少条"
output_model: pdd.ProductRowList
output_model_version: 1
pagination:
  mode: none
  max_pages: 1
retry:
  max_attempts: 1
  step_max_failures: 3
  on_empty_result: accept
observability:
  screenshot: true
  record_llm_io: true
  redact_extra: []
guardrails:
  allowed_domains:
    - "books.toscrape.com"
  prohibited_domains: []
  default_decision: confirm
  max_consecutive_blocks: 3
  rules: []
agent:
  use_vision: false
  max_actions_per_step: 1
  max_steps: 5
  max_failures: 3
"""

# ★ 故意把 params 里的一项拼错（`defualt`）—— `extra="forbid"` 会让它在加载期报错。
#   这条夹具的存在理由见 test_templates_...：坏模板必须**被列出来**，不能静默消失。
BROKEN_TEMPLATE = GOOD_TEMPLATE.replace("    default: 3", "    defualt: 3")


@pytest.fixture()
def env(tmp_path: Path):
    """(build, tmp_path) —— 所有外部依赖都指向 tmp_path。"""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "good.yaml").write_text(GOOD_TEMPLATE, encoding="utf-8")
    (tasks / "broken.yaml").write_text(BROKEN_TEMPLATE, encoding="utf-8")

    async def noop_runner(compiled, *, approver, run_id, events, runs_dir):
        """什么都不做的 runner。★ 这是**默认值**，不是某个用例的便利。"""
        return _outcome(compiled, run_id, runs_dir)

    def build(run_func=None, **kw):
        # ★ 默认值放字典里再 update，这样用例可以用 kw 覆盖任何一项
        #   （比如把 runs_dir 指到一个写不了的路径去测 503）——
        #   直接写成关键字参数的话，覆盖会变成"重复传参"的 TypeError。
        opts: dict = dict(
            runs_dir=tmp_path / "runs",
            tasks_dir=tasks,
            db_path=tmp_path / "test.db",
            # ★★ 默认必须是一个假 runner，**绝不能**是 `run_task`。
            #   不写这一条的话，一个忘了注入的用例会去起**真浏览器 + 真 LLM**：
            #   在这台机器上那是真的花钱、真的点页面，而且它会挂到
            #   300s 的审批超时 —— 表现是"这一整个测试文件跑不完"，
            #   而不是一条失败。一个会花钱的测试是最坏的一类 bug。
            run_func=run_func or noop_runner,
            chrome_path="",       # 空串 = "让库自己探测"，不算降级
            live_llm=True,
        )
        opts.update(kw)
        return create_app(**opts)

    return build, tmp_path


def _outcome(compiled, run_id: str, runs_dir: Path, *, status: str = COMPLETED) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        run_dir=runs_dir / run_id,
        status=status,
        parse_status="ok",
        rows_collected=3,
        record=RunRecord(run_id=run_id, task_id=compiled.task_id, status=status),
    )


@pytest.fixture()
def blocking_runner():
    """一个**会卡在中途**的假 runner，外加放行它的开关。

    ★★ 这是本文件里最关键的一个夹具。它让"run 还在跑"成为一个**可控的状态**，
      而不是一个只能靠 sleep 猜的时间窗口 —— 于是下面那条用例才有可能
      区分这两件事：
        · 服务端**边跑边推**（真流式）；
        · 服务端等 run 跑完、再把事件一次性发出来（假流式）。
      这两者在浏览器里长得一模一样 —— 直到某次 run 要跑三分钟。
    """
    release = threading.Event()
    calls: list[dict] = []

    def make(events_to_publish: int = 2, release_after: float | None = None):
        if release_after is not None:
            # ★ 定时放行（而不是由测试线程放行）：因为读 SSE 的那一步是**阻塞**的，
            #   测试线程在读的时候没法再去 set 这个开关 —— 死锁。
            #   定时器让"run 最早什么时候可能结束"成为一个已知量，
            #   于是"事件到得比它早"就成了可断言的证据。
            threading.Timer(release_after, release.set).start()

        async def runner(compiled, *, approver, run_id, events, runs_dir):
            calls.append({"run_id": run_id, "task_id": compiled.task_id})
            for i in range(1, events_to_publish + 1):
                events.publish(
                    RunEvent(
                        type="step_completed",
                        run_id=run_id,
                        data={"step": i, "url": "https://books.toscrape.com/",
                              "提示": "中文"},
                    )
                )
            # ★ 轮询一个 threading.Event 而不是 asyncio.Event：
            #   测试是同步的（TestClient），拿不到 app 那个事件循环里的对象。
            #   轮询的代价是毫秒级，换来的是"什么时候放行"完全确定。
            while not release.is_set():
                await asyncio.sleep(0.005)
            return _outcome(compiled, run_id, runs_dir)

        return runner

    yield make, release, calls
    release.set()  # 兜底：用例失败时也别把后台任务永远挂在那儿


# ══════════════════════════════════════════════════════════
# 页面与模板
# ══════════════════════════════════════════════════════════
def test_the_single_page_is_shipped(env):
    """★ 单页是**运行时读盘**的（无构建步骤），所以"文件没被部署"是一个
    真实可能的错误。这条用例钉的是"它会随代码一起发布"这件事 ——
    只测 200 是不够的，还要它真的有内容。"""
    build, _ = env
    with TestClient(build()) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "EventSource" in body, "页面里没有 SSE 客户端 —— 拿到的大概是个占位文件"
    assert "stream_gap" in body, "页面的事件名列表与后端 EventType 不同步"


def test_the_deep_link_does_not_mangle_the_run_id(env):
    """★★ 深链 `?run_id=<看板上显示的那串>` 必须能用 —— 而它原先**静默失效**。

    ★ 实测（浏览器里按 F12 之前就先看见了）：打开
      `?run_id=20260917T103341+0000-fb79d9`，看板显示"连接中…"，永远不出来。
      因为 `URLSearchParams` 按**表单**语义解 query string：`+` 就是空格，
      于是前端拿到的 id 是 `20260917T103341 0000-fb79d9` —— 那个目录不存在，
      回放返回空、SSE 没有事件，页面既不报错也不提示，只是"连接中…"。

    ★ 为什么这一定要修：`run_id` 里**天生带 `+`**（`new_run_id()` 的
      `20260917T103341+0000-abcdef` 来自 `+00:00` 时区），看板上那串 id 就是
      给人复制去分享的。一个"复制粘贴就坏、而且坏得不出声"的深链，
      比没有深链更糟 —— 演示时你会以为是服务挂了。

    ★ 这条断言的是**源码形态**而不是行为：项目里没有 JS 测试框架，
      而这类缺陷的形态恰好是"用错了哪个 API"（表单解码 vs 百分号解码），
      所以直接钉住那个 API 的用法是有意义的。
      ⚠️ 它挡不住"换一种方式写错"（比如自己 split 之后再 `decodeURIComponent`
         到一半），那是这一层的极限 —— 老实标注，别装作覆盖了。
    """
    build, _ = env
    with TestClient(build()) as client:
        page = client.get("/").text

    assert "run_id" in page, "页面里没有 run_id 的读取（深链被删了？那是另一种回归）"
    bad = re.search(r"URLSearchParams\s*\(\s*[^)]*location\.search", page)
    assert not bad, (
        f"首页用 URLSearchParams 解 query string（{bad.group(0) if bad else ''}）—— "
        f"它把 `+` 解成空格，而 run_id 里就有 `+`（20260917T103341+0000-abcdef）。\n"
        f"  后果不是报错，是回放**静默**停在'连接中…'。\n"
        f"  正确写法：自己取 `run_id=` 之后那段再做 decodeURIComponent —— "
        f"它只解 %XX，不碰 `+`。"
    )


def test_templates_come_from_the_spec_and_broken_ones_are_listed(env):
    """★★ 表单是**从 TaskSpec 生成**的，不是手写的。

    手写表单的问题不是麻烦，是**会漂移**：YAML 里加了参数、前端不知道，
    于是那个参数永远取默认值 —— 而默认值是"能跑起来"的，
    所以漂移的表现是"功能没生效"，不是"报错"。

    ★ 另一半同样重要：**坏模板必须被列出来**（带上错误原因），不能跳过。
      静默跳过的话，一个语法写错的模板在界面上"不存在"，而它明明躺在
      tasks/ 里 —— 人会去查前端，查一个根本没问题的东西。
    """
    build, _ = env
    with TestClient(build()) as client:
        r = client.get("/api/templates")
    assert r.status_code == 200
    tpls = {t.get("template_id"): t for t in r.json()["templates"]}
    assert set(tpls) == {"good.yaml", "broken.yaml"}, "有模板被静默跳过了"

    good = tpls["good.yaml"]
    assert good["id"] == "test.api_demo"
    # ★ 参数描述是**投影**出来的（不是 pydantic 的 model_json_schema），
    #   所以这里能逐字段断言前端契约。
    p = {x["name"]: x for x in good["params"]}["limit"]
    assert p["type"] == "integer" and p["default"] == 3
    assert p["minimum"] == 1 and p["maximum"] == 10
    assert p["description"] == "最多提取多少条"
    assert good["guardrails"]["default_decision"] == "confirm"

    broken = tpls["broken.yaml"]
    assert "broken" in broken, "坏模板被当成好模板列出来了"
    # ★ 断言错误信息里出现那个**拼错的字段名**：证明是 extra=forbid 抓到的，
    #   而不是别的原因（比如文件读不了）。
    assert "defualt" in broken["broken"], broken["broken"]


def test_template_traversal_is_rejected_without_reading_the_file(env):
    """★★ `template_id` 直接来自请求体，所以必须挡住 `../`。

    不挡的后果不是"读到不该读的文件"这么轻：`load_task` 会把它当 YAML 读，
    读不动就报错 —— 而 PyYAML 的报错里**带着文件内容片段**，
    于是密钥从错误响应里漏出去。这条路短、看着人畜无害，正是容易漏的那类。

    ★ 注意这里断言的是 **400**（输入非法）而不是 404：那个文件确实存在，
      我们拒绝的是这个**形状**的 id。区分这两者，排查方向才不一样。
    """
    build, _ = env
    with TestClient(build()) as client:
        for bad in ("../good.yaml", "sub/good.yaml", ".hidden.yaml", ""):
            r = client.post("/api/runs", json={"template_id": bad, "params": {}})
            assert r.status_code == 400, f"{bad!r} 没被当成非法 id（{r.status_code}）"
        r = client.post("/api/runs", json={"template_id": "nope.yaml", "params": {}})
        assert r.status_code == 404, "不存在的模板应该是 404，不是 400"
        r = client.post("/api/runs", json={"template_id": "good.txt", "params": {}})
        assert r.status_code in (400, 404), "非 YAML 后缀不该被当成模板"


# ══════════════════════════════════════════════════════════
# 起 run
# ══════════════════════════════════════════════════════════
def test_bad_params_are_rejected_before_anything_starts(env, blocking_runner):
    """★ 参数不合法 → 400，而且**runner 一次都没被调用**。

    这条断言（`calls == []`）才是重点：它证明校验发生在**创建浏览器之前**。
    只断言 400 是不够的 —— 一个"先起浏览器、跑起来才发现参数不对"的实现
    同样会返回 400，但它已经烧掉了几次 LLM 调用和一个浏览器进程。
    计划里那条"非法参数不烧 token、不点错按钮"就是这条。
    """
    make, _, calls = blocking_runner
    build, _ = env
    with TestClient(build(run_func=make())) as client:
        r = client.post("/api/runs", json={"template_id": "good.yaml",
                                           "params": {"limit": 999}})
        assert r.status_code == 400
        assert "limit" in r.json()["detail"], r.json()["detail"]
        assert calls == [], "参数非法却已经把 run 跑起来了 —— 校验太晚了"


def test_web_layer_passes_the_login_profile_through(env, monkeypatch, tmp_path):
    """★★ 守卫：登录 profile 这个字段，**Web 这条通道也真的发了**。

    动机是 Phase 6 查出来的一处**不会报错**的缺口：`compile_task` 支持
    `user_data_dir`，CLI 侧传了、Web 侧一直没传。后果是同一份模板、同一个任务，
    CLI 上正常、在看板上**永远停在登录页**：agent 按任务文本第 1 步停下 →
    退出码 0 → report.html 齐全 → sqlite **零行**。

    这正是本项目反复出现的那一类缺陷（"前端读一个字段，某个通道从没发过它"，
    Phase 5 一晚出现过四次），所以它值得一条专门的守卫，而不是"我改过了"。

    ★ 断言的是**注入的 runner 实际收到的东西**（`compiled.browser_kwargs`），
      不是 app.py 里那一行的写法 —— 后者是断言实现，改个写法测试就红；
      而真正要守的是"这个字段确实流到了消费它的地方"。
    """
    build, tmp = env
    profile = tmp / "profile"
    profile.mkdir()
    write_mark(
        profile,
        site="mms.pinduoduo.com",
        logged_in_url="https://mms.pinduoduo.com/goods/goods_list",
        verified_at="2026-09-17T12:00:00+00:00",
    )
    monkeypatch.setattr("webapp.app.USER_DATA_DIR", str(profile))

    seen: dict = {}
    called = threading.Event()

    async def capture(compiled, *, approver, run_id, events, runs_dir):
        seen["browser_kwargs"] = dict(compiled.browser_kwargs)
        called.set()
        return _outcome(compiled, run_id, runs_dir)

    with TestClient(build(run_func=capture)) as client:
        r = client.post("/api/runs", json={"template_id": "good.yaml", "params": {"limit": 3}})
        assert r.status_code == 202, r.text
        assert called.wait(10), "runner 没被调用 —— 后台任务没起来"

    assert seen["browser_kwargs"].get("user_data_dir") == str(profile), (
        f"Web 层没把登录 profile 传下去：{seen['browser_kwargs']}\n"
        "后果不是报错，是看板上跑登录类模板时零行数据。"
    )


def test_a_login_template_without_a_usable_profile_logs_a_warning(env, monkeypatch, tmp_path, caplog):
    """★★ 对照实验：看板起"需要登录"的模板时，服务端必须说话 —— 且**只在真有问题时**说。

    ① `USER_DATA_DIR` 为空（新克隆/CI 的默认）→ 必须有一条 WARNING，
       而且那句话里要含 `check_profile` 给出的可照抄下一步；
    ② 配好且已登录 → **一条 WARNING 都不该有**。

    ★ 没有 ② 的话，"无条件打警告"的实现也能让 ① 通过 —— 而那种实现等于
      把警告变成背景噪声，真出问题时没人会看见。这是本项目所有对照实验的同一条理由。
    """
    build, tmp = env
    (tmp / "tasks" / "login.yaml").write_text(
        GOOD_TEMPLATE.replace("requires_login: false", "requires_login: true"),
        encoding="utf-8",
    )

    async def noop(compiled, *, approver, run_id, events, runs_dir):
        return _outcome(compiled, run_id, runs_dir)

    def start(client) -> None:
        r = client.post("/api/runs", json={"template_id": "login.yaml", "params": {"limit": 3}})
        assert r.status_code == 202, r.text

    def our_warnings() -> list[str]:
        # ★ 只看我们自己的 logger：uvicorn/httpx 的 WARNING 与本用例无关，
        #   把它们算进来会让这条断言变成"环境干不干净"而不是"我们说不说话"。
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "webapp.app" and r.levelno >= logging.WARNING
        ]

    caplog.set_level(logging.WARNING)

    with TestClient(build(run_func=noop)) as client:
        # ① 需要登录，但 profile 没配
        monkeypatch.setattr("webapp.app.USER_DATA_DIR", "")
        caplog.clear()
        start(client)
        hits = [m for m in our_warnings() if "需要登录态" in m]
        assert hits, f"需要登录的模板 + 没配 profile，却一句话都没说：{our_warnings()}"
        assert "ECOM_AGENT_USER_DATA_DIR=" in hits[0], "警告里要给出可照抄的下一步"

        # ② 已登录过（对照）
        ready = tmp / "ready_profile"
        ready.mkdir()
        write_mark(ready, site="s", logged_in_url="u", verified_at="t")
        monkeypatch.setattr("webapp.app.USER_DATA_DIR", str(ready))
        caplog.clear()
        start(client)
        assert our_warnings() == [], "登录态是好的却还在警告 —— 警告会变成没人看的背景噪声"


def test_start_run_returns_before_the_run_finishes(env, blocking_runner):
    """★★ 起 run 必须**立刻**返回 run_id，而不是等到跑完。

    一次 run 以分钟计，中途还会**等人点审批**（默认 300s 超时）。
    同步返回意味着 HTTP 请求挂在那儿，而浏览器/任何中间层都会先超时 ——
    于是"审批还没点，连接已经断了"。

    ★ 判据：runner 还在阻塞中（release 没放行）时，POST 就已经返回了 202。
      用 sleep 猜"应该够快"是测不出这件事的 —— 那个断言在同步实现下
      也会通过（只要 run 恰好很短），于是它什么都没钉住。
    """
    make, release, calls = blocking_runner
    build, _ = env
    app = build(run_func=make())
    with TestClient(app) as client:
        r = client.post("/api/runs", json={"template_id": "good.yaml", "params": {}})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "running"
        assert body["task_id"] == "test.api_demo"
        # ★ 这里要**等** runner 出现，不能直接断言它已经跑到了：
        #   POST 返回的只是"任务已创建"，而创建与任务真正开始执行之间
        #   隔着一次事件循环调度（测试线程跑在另一个线程上）。
        #   直接断言会变成一条偶尔红的用例 —— 而偶发的红比稳定的红更糟，
        #   它会训练人重跑一遍。
        assert _wait_for(lambda: calls or None), "runner 一直没被调用"
        # ★ 判据在这两行：run 还在跑（release 没放行），而 POST 早就返回了、
        #   列表里也已经是 running。同步实现下这里会卡到超时。
        assert not release.is_set()
        assert app.state.registry.get(body["run_id"]).status == "running"
        release.set()
        assert _wait_for(lambda: not calls or
                         app.state.registry.get(body["run_id"]).status != "running"), (
            "放行之后 run 没有走到终态"
        )


def test_a_crashing_run_becomes_an_error_event_not_a_dead_connection(env):
    """★★ 契约：**异常转 SSE `error` 事件，而不是断连。**

    后台任务里抛出的异常如果没人接，会变成一条 asyncio 的
    "Task exception was never retrieved" 日志 —— 而看板那边只会看到
    连接停在那儿不动。人看到的是"卡住了"，实际是任务已经死了。
    这两种表现必须分开，否则排查方向完全不同。

    ★ 同时钉住 `fatal: True`：前端靠它决定"关掉流、标红"，
      而没有这个字段时它会一直显示"实时连接中"。
    """
    build, tmp = env

    async def boom(compiled, *, approver, run_id, events, runs_dir):
        raise RuntimeError("模拟一个跑挂了的 run")

    with TestClient(build(run_func=boom)) as client:
        run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                "params": {}}).json()["run_id"]
        frames = _read_sse(client, run_id)
    kinds = [f["event"] for f in frames]
    assert "error" in kinds, f"run 崩了但流里没有 error 事件：{kinds}"
    err = next(f for f in frames if f["event"] == "error")
    assert err["data"]["kind"] == "RuntimeError"
    assert err["data"]["fatal"] is True
    assert err["data"]["where"] == "run"
    assert "模拟一个跑挂了的 run" in err["data"]["message"]


def test_preflight_failure_is_told_apart_from_a_crash(env):
    """★ 预检失败**不是** "run 崩了"，而是"根本没开跑"。

    两者的处置完全不同（改配置 vs 查现场），所以事件里的 `where` 必须不同。
    合并成一个 `where="run"` 的话，一次配置错误会被读成一次运行期故障 ——
    然后有人会去翻 steps.jsonl 找一个从来没产生过的现场。
    """
    build, tmp = env

    async def refuse(compiled, *, approver, run_id, events, runs_dir):
        raise PreflightError("起点 URL 不在白名单内")

    app = build(run_func=refuse)
    with TestClient(app) as client:
        run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                "params": {}}).json()["run_id"]
        frames = _read_sse(client, run_id)
        live = app.state.registry.get(run_id)
    err = next(f for f in frames if f["event"] == "error")
    assert err["data"]["where"] == "preflight"
    assert live.status == "rejected", "预检失败被记成了别的状态"
    # ★★ 还有一条更能说明"根本没开跑"的证据：**run 目录压根没被创建**。
    #    它意味着没有 steps.jsonl、没有截图、没有 report —— 现场是不存在的，
    #    所以这时候去看现场纯属白跑。这一点值得钉住：一个"先建目录再预检"
    #    的实现会留下一个空目录，而那个空目录会被历史列表列成一个
    #    "incomplete 的 run" —— 于是有人会去查那个 run 到底跑到哪一步死的。
    assert not (tmp / "runs" / run_id).exists(), (
        "预检失败却留下了 run 目录 —— 历史列表里会多出一条查不出所以然的记录"
    )


# ══════════════════════════════════════════════════════════
# SSE
# ══════════════════════════════════════════════════════════
def _parse_sse(lines, *, on_frame=None) -> list[dict]:
    """把 SSE 的裸行解析成 [{event, id, data}]。传输层无关，真服务器和
    TestClient 共用这一份 —— "帧长什么样"只该有一个定义。"""
    frames: list[dict] = []
    cur: dict = {}
    for line in lines:
        if not line:
            if cur:
                frames.append(cur)
                if on_frame is not None:
                    on_frame(cur)
                cur = {}
            continue
        if line.startswith("id: "):
            cur["id"] = int(line[4:])
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
    return frames


def _assert_sse_headers(resp) -> None:
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # ★ 这三个头是 SSE 能不能用的前提，不是可选优化：
    #   少了 no-cache，中间层会缓存住整条流；少了 X-Accel-Buffering，
    #   nginx 会把事件攒成一块再发 —— 表现是"实时看板"变成
    #   "最后一次性全出现"，看起来像后端没推。
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"


def _read_sse(client: TestClient, run_id: str, *, timeout: float = 10.0,
              on_frame=None) -> list[dict]:
    """把一个 SSE 流**读到自然结束**，解析成 [{event, id, data}]。

    ⚠️ 不要在这里做"读几条就 break"的提前退出：TestClient 的流在
       生成器还在产帧时被关闭，关闭动作会去等那个还没结束的响应 ——
       用例会**挂住**而不是变红。挂住的用例没有信息量（而且会被
       归咎于"环境慢"）。需要"提前"判据时用 `on_frame` 记到达时刻（见下）。

    ⚠️ 这条通道**看不到"边跑边推"**（见 `_running_app`）。要用到达时刻做
       判据的用例必须走真服务器。
    """
    with client.stream("GET", f"/api/runs/{run_id}/stream", timeout=timeout) as resp:
        _assert_sse_headers(resp)
        return _parse_sse(resp.iter_lines(), on_frame=on_frame)


@contextlib.contextmanager
def _running_app(app, *, startup_timeout: float = 20.0) -> Iterator[str]:
    """把 app 跑在一个**真 uvicorn** 上，yield 出 base URL。

    ★★ 为什么会有这个东西：**TestClient 看不见流式**。这不是偏好，是能力边界 ——
      starlette 的 TestClient 走进程内 ASGI 调用，它**先把整个应用跑完、
      再把响应拼出来**。于是"帧什么时候到达"在它那里根本不存在：
      一次 2.0s 才结束的 run，它的四条帧会**一起**在 1.98s 出现（实测）。
      也就是说本文件里唯一需要"到达时刻"判据的那条用例，
      恰好是 TestClient 唯一测不了的那一条。
      项目里已有先例（devtools/mock_pdd/server.py 的 running_server），
      这里照它的形状来，不另造机制。

    端口用 0 让 OS 分配：CI 上和别的东西并行跑时，固定端口是会撞的。
    """
    started = threading.Event()
    bound: list[int] = []

    class _StartupSignalingServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            bound.append(self.servers[0].sockets[0].getsockname()[1])
            started.set()

    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            log_level="warning", access_log=False)
    server = _StartupSignalingServer(config)
    thread = threading.Thread(target=server.run, name="webapp-under-test", daemon=True)
    thread.start()
    try:
        if not started.wait(startup_timeout):
            raise RuntimeError(f"测试用的 webapp 没能在 {startup_timeout}s 内起来")
        yield f"http://127.0.0.1:{bound[0]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_the_stream_pushes_while_the_run_is_still_going(env, blocking_runner):
    """★★★ 这条是"实时看板"这四个字的**可执行定义**。

    只有它能把"边跑边推"和"跑完再一次性发"分开 —— 而这两者在浏览器里
    长得一模一样，直到某次 run 要跑三分钟。

    ★ 判据是**到达时刻**，而不是"最终收到了"：
      让 run 卡住 `release_after=2.0` 秒才结束（定时器放行，因为读流的那一步
      是阻塞的，测试线程没法一边读一边放行），然后记录每条帧的到达时间。
      两条 step 是在 run 开始时就发布的，所以：
        · 真流式 → 它们几乎立刻到达（毫秒级）；
        · 假流式 → 它们只能等到 run 结束才出现（2.0s 之后）。
      断言"第 2 条 step 在 1.0s 前就到了"，两种实现在这里必然分开。
      纯靠超时的写法（"读不到就失败"）反而不好：它会把"慢"和"假流式"混为一谈。

    ★ 而且必须**读到流自然结束**，不能读一半就 break：
      TestClient 的流一旦在生成器还在产帧时被关闭，关闭动作会去等那个
      还在跑的响应 —— 表现是用例**挂住**而不是变红。挂住的用例没有信息量。
      （真服务器上这个限制同样成立，且理由更直白：帧还在路上。）

    ★ 本用例走**真 uvicorn**，不走 TestClient —— 理由全部写在 `_running_app`
      的 docstring 里。一句话：TestClient 是先跑完整个应用再拼响应的，
      在它那里"到达时刻"这个量根本不存在。
    """
    make, release, _ = blocking_runner
    build, _ = env
    app = build(run_func=make(events_to_publish=2, release_after=2.0))
    arrived: list[tuple[str, int | None, float]] = []
    with _running_app(app) as base:
        # ★ trust_env=False：这台机器上 HTTPS_PROXY 指着本地 Clash，
        #   让它去"代理" 127.0.0.1 只会得到一个连不上的错误。
        #   局域网/环回地址永远不该走代理，这是配置纪律，不是权宜。
        with httpx.Client(base_url=base, timeout=15.0, trust_env=False) as client:
            run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                    "params": {}}).json()["run_id"]
            t0 = time.monotonic()
            with client.stream("GET", f"/api/runs/{run_id}/stream") as resp:
                _assert_sse_headers(resp)
                frames = _parse_sse(resp.iter_lines(), on_frame=lambda f: arrived.append(
                    (f.get("event"), f.get("id"), time.monotonic() - t0)))

    # ★ 只有两条 step：`run_started` / `run_completed` 是**真 runner**（runner.py）
    #   发的，而这里 runner 是假的 —— 所以它们不该出现。这条断言顺带说明了
    #   分界：Web 层不自己造 run 的事实，它只转发 runner 推上来的东西。
    kinds = [f["event"] for f in frames]
    assert kinds == ["step_completed", "step_completed"], kinds
    steps = [a for a in arrived if a[0] == "step_completed"]
    assert len(steps) == 2
    assert steps[1][2] < 1.0, (
        f"第 2 条 step 在 {steps[1][2]:.2f}s 才到，而这次 run 最早也要 "
        f"{2.0}s 才结束 —— 说明服务端是**等 run 跑完再发的**，不是边跑边推。"
    )
    # ★ 中文必须原样到达：前端直接把元素文本显示给人看，转成 \\uXXXX 就没法看了。
    assert any("中文" in (f["data"].get("提示") or "") for f in frames)
    # ★ `id:` 是断线续传的锚点，实时通道必须有值（回放通道同样有，见 test_events）。
    assert [f["id"] for f in frames] == [1, 2]
    assert release.is_set()


def test_stream_falls_back_to_replay_instead_of_404(env):
    """★ run 不在内存里（进程重启过）时，stream **降级为回放**，不是 404。

    看板刷新页面时不该看到一个错误：它要的东西盘上全都有。
    而"404"会让人以为那个 run 不存在 —— 它明明在 runs/ 里躺着。

    ★ 而且回放通道和实时通道的事件名、载荷形状完全一样（这是"一个渲染器"
      的另一半），所以这里顺带钉住类型序列。
    """
    build, tmp = env
    run_dir = tmp / "runs" / "20260917T000000+0000-abcdef"
    run_dir.mkdir(parents=True)
    (run_dir / "steps.jsonl").write_text(
        json.dumps({"step": 1, "url": "u", "actions": []}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_dir.name, "task_id": "t", "task_name": "历史任务",
                    "status": "completed", "parse_status": "ok", "rows_collected": 1,
                    "steps": 1, "duration_s": 3.0, "errors": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    with TestClient(build()) as client:
        frames = _read_sse(client, run_dir.name)
    assert [f["event"] for f in frames] == [
        "run_started", "step_completed", "run_completed", "stream_gap"
    ] or [f["event"] for f in frames][:3] == [
        "run_started", "step_completed", "run_completed"
    ], [f["event"] for f in frames]
    assert frames[-1]["data"].get("replay") is True, "回放没有标出自己是回放"


def test_stream_404s_for_a_run_that_never_existed(env):
    """★★ 盘上也没有这个 run 时 → 404，而**不是**一条造出来的 run_started。

    这条用例的存在理由是一次实测发现：以前这里会走进回放，而
    `events_from_run_dir` 对一个不存在的目录会产出**一条空的 run_started**
    （task_id 是空串、run_id 取自目录名）。前端于是显示出一个
    「开始过、但一步都没跑」的 run —— 一个**看起来像事实的伪造**。
    排查的人会去查"这次 run 为什么没跑起来"，而该做的是"这个 id 是错的"。

    ★ 所以断言分两层，缺一层就没意义：
      1. 状态码是 404（不是 200）；
      2. **没有任何 run_started 事件**（不是"有事件但内容很空"）。
         只断言第 1 层的话，将来有人把 404 改回 200 却顺手把
         events_from_run_dir 的伪造也去掉了，这条用例会绿 ——
         而那时"不存在"这件事在响应里又变得不可分辨了。

    ★ 与上一条（`..._falls_back_to_replay_instead_of_404`）是**一对**，别合并：
      那条说的是「进程重启过、run 只在盘上」→ 不能 404；
      这条说的是「盘上也没有」→ 必须 404。
      两条合起来才是"这个 run 在不在"的完整定义 —— 只留一条的话，
      无论实现偏向哪边，都有一条用例是绿的。
    """
    build, tmp = env
    with TestClient(build()) as client:
        resp = client.get("/api/runs/20260917T000000+0000-nosuch/stream")
        # ★ 必须真的读一下 body 再判断：不读的话断言只覆盖了状态码，
        #   而"有没有伪造事件"正是这条用例的重点。
        body = resp.read().decode("utf-8")
    assert resp.status_code == 404, (
        f"不存在的 run 返回了 {resp.status_code}，预期 404。\n"
        f"  200 会让前端把「什么都没有」渲染成「这个 run 什么都没跑」—— "
        f"两件完全不同的事长得一样。\n  body={body[:400]}"
    )
    assert "run_started" not in body, (
        f"404 的响应里居然还有 run_started（{body[:400]}）—— 一个不存在的 run "
        f"被造出了「开始过」的记录。"
    )
    assert "找不到 run" in body, (
        f"404 的说明里没有说清是「找不到 run」：{body[:400]}\n"
        f"  前端把 detail 直接显示给人看（resync 失败时），"
        f"所以这句话是给人读的，不能是空的。"
    )
    # ★ 顺带钉住"读操作不产生记录"：一个查不到的 run 不该在 runs/ 下留下目录 ——
    #   留下的话，历史列表会多出一条 incomplete 的、没人能解释的记录。
    assert not (tmp / "runs" / "20260917T000000+0000-nosuch").exists()


# ══════════════════════════════════════════════════════════
# 审批端点
# ══════════════════════════════════════════════════════════
def test_approval_endpoint_wakes_up_the_waiting_approver(env):
    """★★ 这条钉的是 Phase 5 的核心机制：网页点批准 → 拦住的那个 run 继续。

    ★ 它必须走**真的** `WebApprover`（而不是一个桩）：审批能不能被唤醒，
      取决于 `pending/{id}.json` + `asyncio.Event` 这套机制的细节，
      用桩就把被测对象换掉了 —— 那样测的是"我的桩能被唤醒"。
    """
    build, tmp = env

    async def runner(compiled, *, approver, run_id, events, runs_dir):
        # ★ 一个在「等人点审批」的 run。审批请求先脱敏（approver 不再做第二遍）。
        res = await approver.request(
            ApprovalRequest(
                run_id=run_id, step=1, action_name="click",
                element_text="编辑", url="https://books.toscrape.com/",
                rule_id="confirm-edit", reason="写入类操作",
            )
        )
        events.publish(RunEvent(type="approval_resolved", run_id=run_id,
                                data={"outcome": res.outcome.value}))
        return _outcome(compiled, run_id, runs_dir)

    app = build(run_func=runner)
    with TestClient(app) as client:
        run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                "params": {}}).json()["run_id"]
        # ★ 审批 id 从 **registry 里的那个 approver** 上取，而不是查历史列表：
        #   这个假 runner 没有建 run 目录，而历史列表以**盘**为准 ——
        #   于是这个活着的 run 在列表里根本不会出现（这是对的，见 _list_run_dirs）。
        #   取 id 本来就该问"当前在等什么"，而不是问"盘上有什么"。
        assert _wait_for(lambda: app.state.registry.get(run_id)) is not None
        approver = app.state.registry.get(run_id).approver
        aid = _wait_for(lambda: (approver.waiting() or [None])[0])
        assert aid, "run 起来了但没有进入等待审批状态"

        r = client.post(f"/api/runs/{run_id}/approvals/{aid}",
                        json={"approved": True, "approved_by": "测试"})
        assert r.status_code == 200, r.json()
        assert r.json()["approved"] is True

        frames = _read_sse(client, run_id)
    resolved = [f for f in frames if f["event"] == "approval_resolved"]
    assert resolved and resolved[0]["data"]["outcome"] == ApprovalOutcome.APPROVED.value
    # ★ 审批流水必须落在**这个 app 用的 runs 目录**下。
    #   这条断言是有来历的：WebApprover 的两个目录默认取 config.py 的全局常量，
    #   而 create_app 允许注入 runs_dir —— 不把审批目录跟着一起换的话，
    #   一次 run 的证据会被劈成两半（steps.jsonl 在注入目录、审批流水在默认目录），
    #   而默认部署下两者恰好重合，所以这个错在本地永远看不见。
    assert (tmp / "runs" / "approvals" / "decided").is_dir(), (
        "审批流水没有落在注入的 runs 目录下 —— 证据被劈成两半了"
    )


def test_a_denied_approval_actually_denies(env):
    """★★ 点「拒绝」必须**真的**是拒绝 —— 从请求体一路钉到盘上的审批流水。

    ★ 这条测试的来历值得写下来，因为它不是"顺手补一个对称用例"：

      彩排演示时，我在看板上点了一次「拒绝」，卡片却显示 `approved · web`。
      之后同样的操作连做两次都正常，没能复现，**原因至今没查明**。
      当时能确定的只有两件事，而它们各排除了另一半：
        · 直接 `curl -d '{"approved": false}'` → 落盘的是 `"outcome": "denied"`，
          所以**端点**这一侧是对的；
        · 在浏览器里包住 `fetch` 抓包，前端发出去的确实是 `{"approved":false}`，
          所以**前端**这一侧也是对的。
      两半各自都对，合起来出过一次错 —— 这种"没查明"的账不能记成"大概是工具
      手滑了"就翻篇，因为**它的后果是把一次写入放行**。

      所以我做了两件不需要先查明原因就能做的事，这是第一件：
      **把"拒绝"这条分支钉死**。原来端到端只有一条 `approved: True` 的用例 ——
      也就是说，"拒绝"这条分支从来没有被端点级地验证过，
      一个把 false 走成 approved 的反转可以**发布出去而没人知道**。

    ★ 为什么断言要读到 `res.approved`（被唤醒的那个协程拿到的结果），
      而不是只看 HTTP 返回体：返回体是**端点自己说的**，
      而 run 继续往下跑靠的是 approver 返回的那个对象。
      两者不一致时（正是这次要防的形状），只看返回体等于没测。

    ★ 顺带钉住审批流水文件里必须是 `"denied"` 而不是 `"ApprovalOutcome.DENIED"`：
      那是给下游程序解析的，能读但无法比较的字符串等于没写。
    """
    build, tmp = env

    async def runner(compiled, *, approver, run_id, events, runs_dir):
        res = await approver.request(
            ApprovalRequest(
                run_id=run_id, step=1, action_name="click",
                element_text="编辑", url="https://books.toscrape.com/",
                rule_id="confirm-edit", reason="写入类操作",
            )
        )
        # 把**被唤醒的那个协程**看到的结果推出去 —— 它就是 run 继续跑时
        # 用来决定"这个动作做不做"的东西。
        events.publish(RunEvent(type="approval_resolved", run_id=run_id,
                                data={"outcome": res.outcome.value,
                                      "approved": res.approved,
                                      "approved_by": res.approved_by}))
        return _outcome(compiled, run_id, runs_dir)

    app = build(run_func=runner)
    with TestClient(app) as client:
        run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                "params": {}}).json()["run_id"]
        approver = _wait_for(lambda: app.state.registry.get(run_id)
                             and app.state.registry.get(run_id).approver)
        assert approver is not None, "run 没进 registry，等不到审批"
        aid = _wait_for(lambda: (approver.waiting() or [None])[0])
        assert aid, "run 起来了但没有进入等待审批状态"

        r = client.post(f"/api/runs/{run_id}/approvals/{aid}",
                        json={"approved": False, "approved_by": "测试-拒绝"})
        assert r.status_code == 200, r.json()
        assert r.json()["approved"] is False, "端点回显的 approved 不是 False"

        frames = _read_sse(client, run_id)

    resolved = [f for f in frames if f["event"] == "approval_resolved"]
    assert resolved, "没有收到 approval_resolved"
    # ★★ 这三条是这条测试的重点：被唤醒的协程拿到的必须是 denied。
    assert resolved[0]["data"]["outcome"] == ApprovalOutcome.DENIED.value, (
        "点了拒绝，但 run 拿到的结局不是 denied —— 这个反转会把一次写入放行"
    )
    assert resolved[0]["data"]["approved"] is False
    assert resolved[0]["data"]["approved_by"] == "测试-拒绝"

    decided = tmp / "runs" / "approvals" / "decided" / f"{aid}.json"
    assert decided.is_file(), "拒绝也要留下审批流水 —— 没留就等于这次拒绝没发生过"
    payload = json.loads(decided.read_text(encoding="utf-8"))
    assert payload["result"]["outcome"] == "denied", (
        f"流水文件里写的必须是裸字符串 denied，不是 {payload['result']['outcome']!r}"
        "（Enum 直接序列化会变成 'ApprovalOutcome.DENIED'：能读、但没法比较）"
    )
    assert payload["result"]["approved"] is False
    assert payload["result"]["approved_by"] == "测试-拒绝"
    # 请求体原样留在流水里 —— 事后要能回答"人当时到底点了什么"。
    assert payload["rule_id"] == "confirm-edit"
    assert payload["element_text"] == "编辑"


def test_a_late_approval_is_409_not_404(env):
    """★★ 迟到/重复的审批 → **409**，不是 404。

    404 说的是"这个东西不存在"，而这条审批**曾经**存在。区分很重要：
    404 会让人以为"卡片是假的、前端显示了不存在的东西"，于是去查前端；
    409 说的是"你来晚了 —— 它已经超时或被决策过了"，后者才是真实发生的事
    （超时是默认 300s，很容易发生：人离开工位一会儿卡片就失效了）。
    """
    build, _ = env
    with TestClient(build()) as client:
        run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                "params": {}}).json()["run_id"]
        r = client.post(f"/api/runs/{run_id}/approvals/doesnotexist",
                        json={"approved": True})
        assert r.status_code == 409, f"预期 409（你来晚了），实际 {r.status_code}"
        assert "不再等待" in r.json()["detail"] or "已不在等待中" in r.json()["detail"]
        # ★ 而"这个 run 我根本没在跟踪"仍然应该是 404 —— 这两件事不一样。
        r2 = client.post("/api/runs/nosuchrun/approvals/x", json={"approved": True})
        assert r2.status_code == 404


def test_an_ambiguous_approval_body_is_422(env):
    """★ `approved` 必填且**没有默认值**。

    漏了 approved 的请求如果默认成 approve，就是"一句话把危险动作放行了"；
    默认成 deny 又会让人困惑"我明明点了批准"。所以意图不明时**不猜**，422。

    ★ 顺带钉住 `extra="forbid"`：拼错字段名（`approve`）也必须是 422，
      不能被当成"没带 approved"而静默走默认路径。

    ★ 判据用的是 `"maybe"` / `null` 这种**没有真假**的值，而不是 `"yes"`。
      `"yes"` 会被 pydantic 的宽松模式正确转成 True —— 那不是"猜"，
      是"这个值本身就只有一个含义"。把 `"yes"` 也拒掉只会让客户端
      多写一层转换，而危险的是**读不出意图**的那种值。
    """
    build, _ = env
    with TestClient(build()) as client:
        run_id = client.post("/api/runs", json={"template_id": "good.yaml",
                                                "params": {}}).json()["run_id"]
        url = f"/api/runs/{run_id}/approvals/x"
        assert client.post(url, json={"note": "看起来没问题"}).status_code == 422
        assert client.post(url, json={"approve": True}).status_code == 422
        assert client.post(url, json={"approved": "maybe"}).status_code == 422
        assert client.post(url, json={"approved": None}).status_code == 422
        # ★ 而 `"yes"` 是**能被理解**的，所以它该走到业务逻辑里（这里是 409：
        #   这条审批 id 不在等待中），不该被 422 挡在门外。
        assert client.post(url, json={"approved": "yes"}).status_code == 409


def _wait_for(fn, *, timeout: float = 5.0, interval: float = 0.02):
    """轮询到 fn() 返回真值。★ 只在"等另一个线程里的事件循环"时用 ——
    这是同步测试与 app 事件循环之间唯一可靠的会合方式。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


# ══════════════════════════════════════════════════════════
# 历史 / 回放 / 截图
# ══════════════════════════════════════════════════════════
def test_list_runs_is_truthful_about_incomplete_and_skips_approvals(env):
    """★★ 列表以**盘**为准，并且对两种"不像 run 的目录"分别处理：

      · `approvals/` 是审批流水，**不是** run → 不列（列了就是凭空造一个 run）；
      · 没有 `run.json` 的目录 → **列出来并标 incomplete**。
        那种目录的含义是"run 起了但还没来得及写 run.json 就死了"，
        它是一个有诊断价值的事实 —— 跳过它等于把它藏起来，
        而人只会看到"列表里少了一个 run"。
    """
    build, tmp = env
    runs = tmp / "runs"
    (runs / "approvals").mkdir(parents=True)
    (runs / "20260101T000000+0000-aaaaaa").mkdir()
    (runs / "20260101T000000+0000-aaaaaa" / "run.json").write_text(
        json.dumps({"run_id": "20260101T000000+0000-aaaaaa", "task_id": "t",
                    "task_name": "旧的", "status": "completed", "steps": 2}),
        encoding="utf-8",
    )
    (runs / "20260102T000000+0000-bbbbbb").mkdir()  # 半截的：没有任何文件
    with TestClient(build()) as client:
        items = client.get("/api/runs").json()["runs"]
    by_id = {x["run_id"]: x for x in items}
    assert "approvals" not in by_id, "审批流水目录被当成 run 列出来了"
    assert by_id["20260102T000000+0000-bbbbbb"]["incomplete"] is True
    assert by_id["20260101T000000+0000-aaaaaa"]["incomplete"] is False
    # ★ 倒序：run_id 是时间戳形态，字符串序即时间序（new_run_id 保证）。
    assert [x["run_id"] for x in items] == sorted(by_id, reverse=True)


def test_list_runs_carries_the_observed_login_state_per_run(env):
    """★★ 这一条钉的是**列表通道**上的登录态 —— 它是"零行"最常被读到的地方。

    ★ 场景具体是什么：跑一次没登录的采集任务，得到一排
      `completed / empty · 0 行`。人面对这一排的时候要做两个相反的决定 ——
      "去重新扫码" 还是 "这店本来就没数据"。**这两个决定在列表的三个字段里
      长得完全一样**，所以这一列不是锦上添花，它是这一排文字里唯一有信息量的那个。

    ★ 为什么用**两个 run 对照**而不是只断言一个字段存在：
      只断言"有 login_state 这个键"的话，
        · 一个对每一行都写死同一个值的实现（比如全填 ""）照样通过；
        · 而这个字段唯一的作用就是**区分**行与行。
      所以这里造两个形状不同、且**同一次请求里并排返回**的 run，断言它们不同 ——
      一个字段能区分两行，才叫它携带了信息。

    ★ 老 run.json（Phase 6 之前）的对照也在这一条里：它没有这两个键，
      必须回 `""`。⚠️ 退化成 `"logged_in"` 是最坏的一种错 ——
      看板会给"当时根本没人知道登录态已经失效"的历史 run 盖上"已登录"的章，
      而这正好让人**不去**看那几张本来能看出问题的截图。
    """
    build, tmp = env
    runs = tmp / "runs"
    # 落在登录页的那次（就是"零行"两个成因里需要人行动的那一个）
    (runs / "20260102T000000+0000-bbbbbb").mkdir(parents=True)
    (runs / "20260102T000000+0000-bbbbbb" / "run.json").write_text(
        json.dumps({"run_id": "20260102T000000+0000-bbbbbb", "task_id": "t",
                    "task_name": "被登录页挡下的", "status": "completed",
                    "parse_status": "empty", "rows_collected": 0, "steps": 2,
                    "login_state": "login_page",
                    "login_state_reason": "URL 命中登录页特征：/login"}),
        encoding="utf-8",
    )
    # 老的、没探过登录态的
    (runs / "20260101T000000+0000-aaaaaa").mkdir()
    (runs / "20260101T000000+0000-aaaaaa" / "run.json").write_text(
        json.dumps({"run_id": "20260101T000000+0000-aaaaaa", "task_id": "t",
                    "task_name": "上古 run", "status": "completed",
                    "parse_status": "empty", "rows_collected": 0, "steps": 2}),
        encoding="utf-8",
    )
    with TestClient(build()) as client:
        by_id = {x["run_id"]: x for x in client.get("/api/runs").json()["runs"]}
    got = by_id["20260102T000000+0000-bbbbbb"]
    assert got["login_state"] == "login_page", (
        f"列表里的登录态是 {got.get('login_state')!r}，run.json 里写的是 login_page\n"
        f"  这一列空着的时候，这一行和不带登录态的零行 run 长得一模一样。"
    )
    assert got["login_state_reason"] == "URL 命中登录页特征：/login"
    # ★ 对照一：老 run.json → ""（不是被填成任何一个真判决）
    old = by_id["20260101T000000+0000-aaaaaa"]
    assert old["login_state"] == ""
    assert old["login_state"] not in ("logged_in", "login_page", "unknown")
    # ★ 对照二：两行并排，值必须不同 —— 证明这一列真的按 run 走，不是写死的常量。
    assert got["login_state"] != old["login_state"]


def test_get_run_returns_replay_events_and_404s_for_unknown(env):
    """★ `GET /api/runs/{id}` 同时是 SSE 的**兜底**：前端收到 `stream_gap`
    或发现 seq 跳号时就来这里拉全量。所以它返回的事件必须和 SSE 同形状 ——
    否则"补数据"就需要第二套解析逻辑。"""
    build, tmp = env
    run_dir = tmp / "runs" / "20260101T000000+0000-cccccc"
    run_dir.mkdir(parents=True)
    (run_dir / "steps.jsonl").write_text(
        json.dumps({"step": 1, "actions": [], "guardrail_decisions": []},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_dir.name, "task_id": "t", "status": "completed",
                    "steps": 1, "rows_collected": 0, "errors": []}), encoding="utf-8")
    with TestClient(build()) as client:
        j = client.get(f"/api/runs/{run_dir.name}").json()
        assert j["live"] is None
        assert [e["type"] for e in j["events"]] == [
            "run_started", "step_completed", "run_completed"]
        # ★ 事件是**完整的** RunEvent（含 seq / at / run_id），前端直接喂给
        #   同一个 handleEvent —— 少一个字段就得在客户端补，那就是第二套逻辑。
        assert set(j["events"][0]) >= {"type", "run_id", "seq", "at", "data"}
        assert j["record"]["task_id"] == "t"
        assert client.get("/api/runs/nosuchrun").status_code == 404


def test_screenshot_path_comes_from_the_record_and_cannot_escape(env):
    """★★ 三个分支，每个都对应一个真实会发生的状态：

      1. 记录里没有截图路径 → 404 "这一步没有截图记录"（不是 500）；
      2. 记录里有路径但文件不在 → 404 且**说清楚这不是 bug**
         （图不是真 PNG 时 recorder 刻意不落盘：宁可缺一张图，
          也不给一张打开才发现坏掉的图）；
      3. 路径越界 → 400。挡它的理由不是"我们的记录可能被篡改"，
         而是这条路径最终来自**盘上的文件内容**，而盘上的文件任何能写
         runs/ 的进程都能改 —— 一个 `../../.env` 就够读到密钥了。

    ★ 还有一条不在这条用例里但同样承重：路径是从**这一步自己的记录**里读的，
      不是拼的（截图按 attempt 分子目录，拼的话重试过的 run 会稳定返回
      第一次尝试的图）。见 test_mock_pdd_e2e 的真跑。
    """
    build, tmp = env
    run_dir = tmp / "runs" / "20260101T000000+0000-dddddd"
    (run_dir / "screenshots").mkdir(parents=True)
    png = run_dir / "screenshots" / "step-001.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (run_dir / "steps.jsonl").write_text(
        "\n".join([
            json.dumps({"step": 1, "screenshot_path": "screenshots/step-001.png"}),
            json.dumps({"step": 2, "screenshot_path": ""}),
            json.dumps({"step": 3, "screenshot_path": "screenshots/gone.png"}),
            json.dumps({"step": 4, "screenshot_path": "../../secret.txt"}),
        ]) + "\n", encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps({"run_id": run_dir.name}), encoding="utf-8")
    (tmp / "secret.txt").write_text("不该被读到", encoding="utf-8")

    with TestClient(build()) as client:
        ok = client.get(f"/api/runs/{run_dir.name}/steps/1/screenshot")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "image/png"
        assert ok.content.startswith(b"\x89PNG")

        assert client.get(
            f"/api/runs/{run_dir.name}/steps/2/screenshot").status_code == 404
        gone = client.get(f"/api/runs/{run_dir.name}/steps/3/screenshot")
        assert gone.status_code == 404
        assert "reproducer" not in gone.json()["detail"]
        assert "不是 bug" in gone.json()["detail"], (
            "记录里有路径但文件不在时，报错必须说清这是一个被刻意接受的状态 —— "
            "否则下一个人会去查 recorder 的 bug"
        )
        esc = client.get(f"/api/runs/{run_dir.name}/steps/4/screenshot")
        assert esc.status_code == 400, "越界路径没被挡住"
        assert "不该被读到" not in esc.text


# ══════════════════════════════════════════════════════════
# 健康检查：两档
# ══════════════════════════════════════════════════════════
def test_health_ok_when_everything_is_fine(env):
    """★ 空串的 chrome 路径**不算降级**：它是"明确要求库自己探测"，
    与"没配"是两件事（config.py 里区分了）。报成 degraded 会训练人忽略这个字段。"""
    build, _ = env
    with TestClient(build()) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["soft"]["chrome"]["ok"] is True
    assert j["hard"]["db_writable"]["ok"] is True


def test_soft_dependency_failure_is_200_degraded(env, tmp_path):
    """★★ 软依赖坏（浏览器路径不对、没开 LLM）→ **200 + degraded**，不是 503。

    理由：服务本身是好的 —— **可以列历史、可以回放、可以审批**，
    只是起不了新 run。把软依赖也判成 503 会让"看历史"被误杀，
    而"浏览器起不来时还能回放证据"恰恰是审计场景最需要的。

    ★ 同时钉住 `live: False` 与 `ok: False` 同时出现：这个组合正是
      "能看不能跑"，前端要据此把"开始"按钮灰掉而不是报错。
    """
    build, _ = env
    with TestClient(build(chrome_path=str(tmp_path / "nope" / "chrome.exe"),
                          live_llm=False)) as client:
        r = client.get("/api/health")
    assert r.status_code == 200, "软依赖坏被当成了 503 —— 看历史会被误杀"
    j = r.json()
    assert j["status"] == "degraded"
    assert j["soft"]["chrome"]["ok"] is False
    assert j["soft"]["llm"] == {
        "ok": False, "live": False,
        "hint": "未开启真调 LLM：可看历史与回放，但起不了新 run",
    }
    assert all(v["ok"] for v in j["hard"].values()), "硬依赖被误判成坏了"


def test_hard_dependency_failure_is_503(env, tmp_path):
    """★★ 硬依赖坏（写不了盘）→ 503 + `unhealthy`。

    这条必须**真的**制造一个写不了盘的目录，而不是 mock 掉：
    做法是让 runs_dir 指向一个**已经存在的普通文件** ——
    这样 `mkdir(parents=True, exist_ok=True)` 会抛
    `FileExistsError`（OSError 的子类），与"只读挂载"是同一类失败。

    ★ 判据是 503 **且** hard 里那一项 ok=False：只看状态码的话，
      一个"把任何异常都判成 503"的实现也会通过（而它会把软依赖也误杀）。
    """
    build, _ = env
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")
    with TestClient(build(runs_dir=blocker)) as client:
        r = client.get("/api/health")
    assert r.status_code == 503
    j = r.json()
    assert j["status"] == "unhealthy"
    assert j["hard"]["runs_dir_writable"]["ok"] is False
    assert j["hard"]["runs_dir_writable"]["error"], "坏了但没给出原因，排查会卡住"
