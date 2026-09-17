"""report.html —— 给人看的报告。

★ 为什么是原生 HTML 而不是模板引擎 / Streamlit：
  产物要能**双击打开**、能被 git 存、能在没有 Python 的机器上看。
  一个需要起服务才能看的"报告"，在面试现场就是打不开的那种。

★ 渲染做成**纯函数**（`render_report` 返回字符串）：
  报告最容易出的错是"少了一列""某个标记没出现"，而这些恰好可以用
  字符串断言直接测 —— 只要渲染不需要文件系统、不需要浏览器。
  `write_report` 只是把纯函数的结果写盘。

★★ 安全：**页面内容会进这份 HTML**（元素文本、URL、模型思考）。
   所以每一处插值都必须 `esc()`。这不是洁癖 ——
   一个 mock 站点上写着 `<script>` 的商品标题，会让报告自己变成注入点，
   而报告是"我们拿来证明护栏有效"的东西。护不住自己就没人信它护得住别人。
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Iterable

from ecom_agent.observability.models import (
    BLOCKED,
    COMPLETED,
    FAILED,
    RunRecord,
    StepRecord,
)

# 状态 → 颜色。★ 用显式映射而不是 if 链：加一个新状态时忘了改这里，
# 报告会显示成"灰的"，那看起来像"没状态"而不是"渲染漏了"。
_STATUS_STYLE = {
    COMPLETED: ("#0b7a3b", "#e6f6ec"),
    BLOCKED: ("#8a5a00", "#fff5e0"),
    FAILED: ("#a11", "#fdeaea"),
    "running": ("#444", "#eee"),
}


def esc(v: Any) -> str:
    """HTML 转义。★ 所有插值都必须过这个函数。"""
    return html.escape("" if v is None else str(v))


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;'
        f'font-size:12px;color:{color};background:{bg};border:1px solid {color}33">{esc(text)}</span>'
    )


def _stat_row(label: str, value: Any, note: str = "") -> str:
    n = f'<div class="note">{esc(note)}</div>' if note else ""
    return f'<tr><th>{esc(label)}</th><td>{esc(value)}{n}</td></tr>'


def render_report(
    record: RunRecord,
    steps: Iterable[StepRecord],
    *,
    run_dir: Path | None = None,
) -> str:
    """渲染报告。纯函数：同样的输入永远给同样的 HTML。"""
    steps = list(steps)
    color, bg = _STATUS_STYLE.get(record.status, ("#444", "#eee"))

    parts: list[str] = [
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>",
        f"<title>run {esc(record.run_id)} · {esc(record.task_id)}</title>",
        _CSS,
        "</head><body>",
    ]

    # ── 头部 ──────────────────────────────────────────────
    parts.append("<header>")
    parts.append(f"<h1>{esc(record.task_name or record.task_id)}</h1>")
    parts.append(
        f"<div class='sub'>{_badge(record.status, color, bg)} "
        f"<code>{esc(record.task_id)}</code> · run <code>{esc(record.run_id)}</code>"
        f" · 第 {record.attempt} 次尝试</div>"
    )
    parts.append("</header>")

    # ── 安全与观测自身的健康度 ────────────────────────────
    # ★ 这两块【放在最前面】，排在结果前面。
    #   理由：这份报告的第一读者是"要判断这次运行可不可信"的人。
    #   一张有 12 行数据的漂亮结果，如果护栏一步都没判（selector_map 空），
    #   或者截图坏了一半，那 12 行数据的可信度是 0 —— 而这个判断必须在看数据【之前】做。
    parts.append("<section><h2>可信度（先看这块）</h2><table>")
    parts.append(
        _stat_row(
            "护栏判定",
            f"{sum(1 for s in steps for _ in s.guardrail_decisions)} 条",
            "判定数为 0 时，看下面的『判定缺失步』和『元素缺失步』——"
            "那通常意味着护栏压根没拿到元素文本",
        )
    )
    parts.append(
        _stat_row(
            "判定缺失步",
            ", ".join(str(s) for s in record.snapshot_missing_steps) or "无",
            "这些步没有拿到回调快照 → 没有 URL/截图，也就没有可判定的元素文本",
        )
    )
    parts.append(
        _stat_row(
            "元素缺失步",
            ", ".join(str(s) for s in record.empty_selector_map_steps) or "无",
            "★ 这些步的 selector_map 为空 → 护栏一条规则都匹配不上 → 而『没匹配上』"
            "会落到 default_decision，是一个**合法结果**（不报错、不告警）。"
            "可能是页面真没元素，也可能是观测失效（库把 map 原地清空了）——"
            "两种在这里长得一模一样，所以要和这些步的动作并排看",
        )
    )
    parts.append(
        _stat_row(
            "快照覆盖次数",
            record.snapshot_overwrites,
            "非 0 说明回调与钩子的配对时序和假设不符，记录可能整体错位一帧",
        )
    )
    parts.append(
        _stat_row(
            "截图",
            f"{record.screenshot_count} 张",
            ", ".join(str(s) for s in record.same_frame_steps) and
            f"画面与上一步完全相同的步：{', '.join(str(s) for s in record.same_frame_steps)}"
            "（★ 这是原始信号，不是故障 —— 要和动作名一起看）"
            or "",
        )
    )
    parts.append(
        _stat_row(
            "脱敏",
            "，".join(f"{k}={v}" for k, v in sorted(record.redaction_counts.items())) or "未发生",
            "全为『未发生』而页面上明明有会话信息，说明词表或规则失效了",
        )
    )
    if record.unsafe_auto_approved:
        parts.append(
            _stat_row(
                "⚠️ 调试后门",
                "本次使用了自动放行审批通道",
                "这条记录的意义就是让『调试时关掉审批』在真实场景里留下痕迹",
            )
        )
    parts.append("</table></section>")

    # ── 结果 ──────────────────────────────────────────────
    parts.append("<section><h2>结果</h2><table>")
    parts.append(_stat_row("解析状态", record.parse_status))
    parts.append(_stat_row("采集行数", record.rows_collected))
    parts.append(_stat_row("护栏策略指纹", record.task_fingerprint[:16]))
    parts.append(_stat_row("browser-use", record.browser_use_version))
    parts.append(_stat_row("模型", f"{record.model}（{record.provider}）"))
    parts.append(_stat_row("步数", record.steps))
    parts.append(_stat_row("LLM 调用", record.llm.summary()))
    parts.append(_stat_row("耗时", f"{record.duration_s:.1f}s"))
    parts.append("</table>")

    if record.sanity_flags:
        # ★ 可疑标记：只展示不删除。删掉就再也看不到"LLM 出错的方式"。
        parts.append("<h3>可疑数据（已标记，未删除）</h3><ul class='flags'>")
        for gid, flags in record.sanity_flags.items():
            parts.append(f"<li><code>{esc(gid)}</code>：{esc('、'.join(flags))}</li>")
        parts.append("</ul>")

    if record.errors:
        parts.append("<h3>错误</h3><ul class='errors'>")
        for e in record.errors:
            parts.append(f"<li>{esc(e)}</li>")
        parts.append("</ul>")
    parts.append("</section>")

    # ── 时间线 ────────────────────────────────────────────
    parts.append("<section><h2>执行时间线</h2>")
    for s in steps:
        parts.append(_render_step(s))
    if not steps:
        parts.append("<p class='note'>没有步进记录。</p>")
    parts.append("</section>")

    parts.append(
        "<footer><p>本报告由 ecom-agent 生成。所有数据来自任务模板指定的站点；"
        "演示仓库中不含真实店铺数据。日志已按 <code>observability.redact_extra</code> "
        "与内置规则（手机号 / 邮箱 / 身份证 / 会话凭证）脱敏。</p>"
        "<p>本报告<strong>不</strong>包含：库自己落在系统临时目录里的截图"
        "（关机即失，已另存）、未脱敏的原始模型输入输出。</p></footer>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_step(s: StepRecord) -> str:
    """渲染一步。

    ★★ `same_frame_as_previous` 必须和【动作名】并排显示，不能单独标成"点击无效"。
      S5 实测：某步 sha 与上一步完全相同，而那步的动作是 `scroll`，
      日志明写 "Scrolled down 1080px" —— 它成功执行了，只是页面比视口短、滚不动。
      同一句话用在 `click` 上就值得怀疑。**信号是"画面没变"，是不是问题取决于动作。**
    """
    names = "、".join(a.name for a in s.actions) or "（无动作）"
    same = (
        '<span class="warn">画面与上一步相同</span>' if s.same_frame_as_previous else ""
    )
    # ★★ 中止标记必须出现在【摘要行】上，不能只写在展开后的正文里。
    #   理由：这些记录的默认折叠状态就是"看着一切正常"，而读者是靠扫摘要行
    #   决定展开哪一步的。标记藏在正文里，等于要求人先展开一个他认为没问题的步骤。
    aborted = (
        '<span class="abort">本步动作未执行</span>' if s.actions_not_executed else ""
    )

    rows: list[str] = []
    for a in s.actions:
        txt = f"「{esc(a.element_text)}」" if a.element_text else "<span class='note'>（取不到元素文本）</span>"
        rows.append(f"<li><code>{esc(a.name)}</code> {txt} <span class='note'>{esc(a.params)}</span></li>")
    for r in s.results:
        bits = []
        if r.is_done:
            bits.append("done")
        if r.success is not None:
            bits.append(f"success={r.success}")
        if r.error:
            bits.append(f"<span class='err'>{esc(r.error)}</span>")
        if r.extracted_preview:
            bits.append(f"<span class='note'>{esc(r.extracted_preview)}</span>")
        if bits:
            rows.append(f"<li>{' · '.join(bits)}</li>")

    # ★ 只在真的要读结果时，才把"这一行的结果不可信"放在结果【上面】。
    #   放在下面的话，读者已经读完结果了才看到警告 —— 顺序错了，
    #   而人对"先看到的内容"的信任是在看到警告之前就打好的。
    stale = (
        "<div class='decisions'><h4>本步的结果不可信</h4>"
        "<p>这一步被护栏硬停中止了，<strong>动作一个都没执行</strong>。"
        "库仍然为它记了一行（<code>_execute_step</code> 无条件调 <code>on_step_end</code>），"
        "而这一行的 <code>results</code> 是<strong>上一步的残留</strong> ——"
        "它看起来完全正常，但对不上。要看真正的结果请回到上一步。</p></div>"
        if s.actions_not_executed
        else ""
    )

    decisions = ""
    if s.guardrail_decisions:
        items = "".join(
            f"<li><code>{esc(d.rule_id or 'default')}</code> → <strong>{esc(d.decision)}</strong>"
            f"：{esc(d.reason)}"
            + (f"（审批：{'通过' if d.approved else '拒绝'}"
               f"{'，审批人=' + esc(d.approved_by) if d.approved_by else ''}）" if d.approved is not None else "")
            + "</li>"
            for d in s.guardrail_decisions
        )
        decisions = f"<div class='decisions'><h4>护栏判定</h4><ul>{items}</ul></div>"

    shot = (
        f"<figure><img src='{esc(s.screenshot_path)}' alt='step {s.step} 截图' loading='lazy'>"
        f"<figcaption><code>{esc(s.screenshot_sha256 or '')[:12]}</code></figcaption></figure>"
        if s.screenshot_path
        else "<div class='note'>（本步无截图）</div>"
    )

    return f"""
<details class="step" {'open' if s.guardrail_decisions or s.results else ''}>
  <summary><span class="sn">#{s.step}</span> <span class="acts">{esc(names)}</span> {same}{aborted}
    <span class="note">{s.duration_s:.2f}s · {esc(s.url)}</span></summary>
  <div class="body">
    <div class="meta">{esc(s.started_at)}{f' · 第 {s.attempt} 次尝试' if s.attempt > 1 else ''}</div>
    {f'<div class="thought"><h4>模型思考</h4><p>{esc(s.model_thought)}</p></div>' if s.model_thought else ''}
    {stale}
    <ul class="rows">{''.join(rows)}</ul>
    {decisions}
    {shot}
  </div>
</details>"""


def write_report(path: Path, html_text: str) -> Path:
    """写盘。★ 显式 utf-8 —— Windows 默认编码会把中文报告写成乱码。"""
    path.write_text(html_text, encoding="utf-8")
    return path


_CSS = """<style>
:root{--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--bg:#fff;--acc:#0b5fff}
*{box-sizing:border-box}
body{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  color:var(--fg);background:#fafafa;margin:0;padding:24px;max-width:1100px;margin:0 auto}
header{border-bottom:2px solid var(--line);padding-bottom:12px;margin-bottom:20px}
h1{font-size:20px;margin:0 0 6px}
h2{font-size:16px;margin:24px 0 10px;padding-left:8px;border-left:3px solid var(--acc)}
h3,h4{font-size:14px;margin:14px 0 6px}
.sub{color:var(--mut);font-size:13px}
code{background:#f0f0f3;padding:1px 5px;border-radius:3px;font-size:12px;
  font-family:ui-monospace,Consolas,monospace}
table{border-collapse:collapse;width:100%;background:var(--bg);border:1px solid var(--line)}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{width:170px;color:var(--mut);font-weight:500;background:#fcfcfd}
.note{color:var(--mut);font-size:12px}
.step{background:var(--bg);border:1px solid var(--line);border-radius:6px;margin:8px 0}
.step>summary{padding:8px 12px;cursor:pointer;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.step .body{padding:10px 14px;border-top:1px solid var(--line)}
.sn{font-weight:600;color:var(--acc)}
.acts{font-weight:500}
.warn{color:#8a5a00;background:#fff5e0;border:1px solid #8a5a0033;
  border-radius:10px;padding:1px 8px;font-size:12px}
.abort{color:#a11;background:#fdeaea;border:1px solid #a11a1133;
  border-radius:10px;padding:1px 8px;font-size:12px;font-weight:600}
.err{color:#a11}
.rows{margin:6px 0;padding-left:18px}
.decisions{margin:10px 0;padding:8px 12px;background:#fff8f0;border-left:3px solid #d97706;border-radius:4px}
.decisions ul{margin:4px 0;padding-left:18px}
.flags,.errors{padding-left:18px}
.flags li{color:#8a5a00}
.errors li{color:#a11}
figure{margin:10px 0}
figure img{max-width:100%;border:1px solid var(--line);border-radius:4px;display:block}
figcaption{color:var(--mut);font-size:11px;margin-top:2px}
footer{margin-top:28px;padding-top:12px;border-top:1px solid var(--line);color:var(--mut);font-size:12px}
.thought p{margin:4px 0;color:#333}
</style>"""
