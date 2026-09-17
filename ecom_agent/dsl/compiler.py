"""TaskSpec + 参数 → CompiledTask。

★ 纯函数：无 IO、无浏览器、无网络、无 async。
  这是整个项目"能被测试覆盖"的基石 —— 编译产物的每一部分都可以直接断言。

★ "每个字段都有独立的强制点"就体现在这个函数的输出里：

    | 产物            | 谁强制它                                    |
    |-----------------|---------------------------------------------|
    | params          | pydantic + 本模块的 _coerce_params          |
    | guardrails      | GuardrailPolicy（运行期拦截）               |
    | output_model    | pydantic（校验 LLM 产出）                   |
    | pagination      | 计数器（max_pages 是硬闸）                  |
    | max_steps       | browser-use 自己的步数上限                  |

  而一段自然语言 prompt 的所有约束都只有【一个】强制点：LLM 愿不愿意听。
  DSL 的本质是把「希望 LLM 做的事」和「不管 LLM 做什么都必须成立的事」分离开。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ecom_agent.dsl.models import PLACEHOLDER_RE, ParamSpec, TaskSpec
from ecom_agent.dsl.registry import resolve_output_model
from ecom_agent.guardrails.policy import GuardrailPolicy
from ecom_agent.guardrails.rules import Decision


class ParamError(ValueError):
    """任务参数不合法。★ 这个异常必须在创建浏览器之前抛出。"""


@dataclass(frozen=True)
class CompiledTask:
    """一份可以直接交给 runner 的编译产物。"""

    spec: TaskSpec
    params: dict[str, Any]
    task_text: str
    output_model: type[BaseModel]
    browser_kwargs: dict[str, Any]
    agent_kwargs: dict[str, Any]
    run_kwargs: dict[str, Any]
    policy: GuardrailPolicy
    fingerprint: str
    guardrail_clauses: list[str] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return self.spec.id

    def describe(self) -> str:
        """给人看的编译摘要（Phase 1 的验收就是打印它）。"""
        lines = [
            f"任务      : {self.spec.id}  ({self.spec.name})",
            f"指纹      : {self.fingerprint[:16]}",
            f"参数      : {self.params}",
            f"输出模型  : {self.spec.output_model}@v{self.spec.output_model_version}",
            f"浏览器    : {self.browser_kwargs}",
            f"Agent     : {self.agent_kwargs}",
            f"run       : {self.run_kwargs}",
            f"护栏条款  : {len(self.guardrail_clauses)} 条",
            "── 下发给 LLM 的任务文本 ──",
            self.task_text,
        ]
        return "\n".join(lines)


# ── 参数强制 ──────────────────────────────────────────────
def _coerce_params(spec: TaskSpec, provided: dict[str, Any] | None) -> dict[str, Any]:
    """校验并归一化调用方给的参数。

    ★ 顺序很关键：这个函数在【创建浏览器之前】跑完。
      非法参数不该烧 token，更不该让浏览器点错按钮 —— 校验位置本身就是安全设计。
    """
    provided = dict(provided or {})
    unknown = set(provided) - set(spec.params)
    if unknown:
        raise ParamError(f"未知参数 {sorted(unknown)}；本任务接受 {sorted(spec.params)}")

    out: dict[str, Any] = {}
    for name, ps in spec.params.items():
        if name in provided:
            out[name] = _check_one(name, ps, provided[name])
        elif ps.default is not None:
            out[name] = ps.default
        elif ps.required:
            raise ParamError(f"缺少必填参数 {name!r}（{ps.description or '无说明'}）")
        else:
            out[name] = None
    return out


def _check_one(name: str, ps: ParamSpec, value: Any) -> Any:
    if ps.type == "enum":
        if value not in (ps.enum or []):
            raise ParamError(f"参数 {name}={value!r} 不在允许值 {ps.enum} 内")
        return value

    if ps.type in ("integer", "number"):
        # ★ bool 必须在 int 之前拦掉：Python 里 isinstance(True, int) 是 True，
        #   不拦的话 limit=true 会被当成 1 —— 一个静默的错误输入变成有效值。
        if isinstance(value, bool):
            raise ParamError(f"参数 {name} 需要 {ps.type}，收到布尔值 {value!r}")
        try:
            num = int(value) if ps.type == "integer" else float(value)
        except (TypeError, ValueError) as e:
            raise ParamError(f"参数 {name} 需要 {ps.type}，收到 {value!r}") from e
        if ps.ge is not None and num < ps.ge:
            raise ParamError(f"参数 {name}={num} 小于下限 {ps.ge}")
        if ps.le is not None and num > ps.le:
            raise ParamError(f"参数 {name}={num} 大于上限 {ps.le}")
        return num

    if ps.type == "boolean":
        if isinstance(value, bool):
            return value
        raise ParamError(f"参数 {name} 需要 boolean，收到 {value!r}")

    if not isinstance(value, str):
        raise ParamError(f"参数 {name} 需要 string，收到 {type(value).__name__}")
    return value


# ── 任务文本渲染 ──────────────────────────────────────────
def _substitute(text: str, params: dict[str, Any]) -> str:
    """把 {name} 换成参数值。

    ★ 为什么不用 str.format：任务文本是人写的散文，里面完全可能出现
      与参数无关的花括号（一个 JSON 片段、一段正则示例）。format 遇到它们
      会抛 KeyError/IndexError —— 于是"模板里写了个花括号"变成"编译崩了"。
      这里只替换【认得出的参数名】，其余花括号原样留着，失败是软性的。

    ★ 值为 None 时保留占位符原文，不渲染成字符串 "None"。
      渲染成 "None" 会造出一句语法通顺、语义有害的指令（"按关键词「None」搜索"），
      而保留 `{keyword}` 至少诚实地显示"这个位置没填上"。
      （正常路径下这种情况已被 TaskSpec 的占位符校验挡在加载期，
       这里是给直接构造 TaskSpec 的调用方兜底。）
    """

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params or params[name] is None:
            return m.group(0)
        return str(params[name])

    return PLACEHOLDER_RE.sub(repl, text)


def _render_task_text(spec: TaskSpec, params: dict[str, Any], clauses: list[str]) -> str:
    """把 TaskSpec 渲染成下发给 LLM 的文本。

    ⚠️ 【不要】在这里拼 JSON schema。
      Agent.__init__ 会通过 _enhance_task_with_schema 自己拼一遍。
      自己再拼会让 schema 在提示词里出现两次 —— 既浪费 token，
      又制造了"两处 schema 版本不一致"的可能（只改了一处的那种）。

    ★ 护栏条款【要】拼进来（已批准的计划里就是这么定的）。
      让 LLM 提前知道"删除类操作会被系统拒绝"，能显著减少无效尝试 ——
      它撞一次护栏 = 一步的 token + 一次审批打扰（或一次拒绝日志）。
      代价是提示词变长，以及护栏规则一改、task_fingerprint 就变。
      后者其实是【正确】的：不同的策略确实就是不同的任务定义，
      指纹相同而策略不同才是真正危险的（回放时会用错策略）。

    ★ 占位符替换的范围由 spec.templated_texts() 决定，与加载期的校验同源。
      见 models.TaskSpec.templated_texts 的说明 —— 两边各写一份字段清单
      必然漂移，而漂移的后果是静默的。
    """
    sub = lambda s: _substitute(s, params)  # noqa: E731
    parts: list[str] = [sub(spec.goal).strip(), ""]

    parts.append("执行步骤：")
    for i, s in enumerate(spec.steps, 1):
        parts.append(f"{i}. {sub(s).strip()}")

    if params:
        # ★ "本次参数"块保留，即使上面的占位符已经替换过。
        #   两者不是重复：替换后参数散落在句子里（人读着顺），
        #   这个块是一次性看全所有取值的清单（模型对齐用）。
        parts.append("")
        parts.append("本次参数：")
        for k, v in params.items():
            if v is not None:
                parts.append(f"- {k} = {v}")

    if spec.pagination.mode != "none":
        parts.append("")
        parts.append("分页：")
        parts.append(f"- 最多翻 {spec.pagination.max_pages} 页，达到上限立即停止。")
        if spec.pagination.next_selector_hint:
            # ★ 这段是【语义提示】不是 CSS 选择器 —— 刻意用自然语言描述位置，
            #   因为真实站点的 class 是构建产物 hash，会随发版变。
            parts.append(f"- “下一页”按钮的位置：{sub(spec.pagination.next_selector_hint)}")
        if spec.pagination.stop_when:
            parts.append(f"- 停止条件：{sub(spec.pagination.stop_when)}")

    if spec.requires_login:
        parts.append("")
        parts.append(
            "登录：如遇登录页或扫码页，立即停止并用 done 汇报“需要人工登录”，"
            "不要尝试输入账号密码、不要尝试绕过验证。"
        )

    if clauses:
        parts.append("")
        parts.append("系统护栏（由程序强制，不是建议）：")
        parts.extend(f"- {c}" for c in clauses)

    return "\n".join(parts)


def _render_guardrail_clauses(spec: TaskSpec) -> list[str]:
    """把规则集渲染成人话，供提示词和文档复用。

    ★ 只渲染 block / confirm 两类。
      allow 规则是"默认就允许的东西"，写进提示词没有信息量，
      反而会把真正重要的禁令淹没在一堆"你可以搜索、你可以翻页"里。
    """
    clauses: list[str] = []
    for r in sorted(spec.guardrails.rules, key=lambda x: (-x.decision.severity, x.id)):
        if r.decision is Decision.ALLOW:
            continue
        verb = "会被系统直接拒绝" if r.decision is Decision.BLOCK else "会暂停并请求人工确认"
        what = r.match_element_text or r.match_param_regex or r.match_url or "某些操作"
        acts = "、".join(r.match_action) if r.match_action else "任意动作"
        clauses.append(f"涉及「{what}」的 {acts} {verb}。原因：{r.reason or r.id}")

    if spec.guardrails.default_decision is Decision.CONFIRM:
        clauses.append("未在上面列出的任何操作，都会暂停并请求人工确认 —— 不要假设它会被放行。")
    return clauses


# ── 指纹 ──────────────────────────────────────────────────
def compute_fingerprint(spec: TaskSpec, params: dict[str, Any]) -> str:
    """任务定义 + 参数 的稳定哈希，落库做可复现追溯。

    ★ 必须包含护栏（guardrails 是 spec 的一部分，所以自动包含）。
      指纹相同而策略不同是最危险的情况：回放时会用当前策略去解释一次
      按旧策略执行的记录，于是报告里显示的"当时为什么放行"是错的。

    ★ 用 sort_keys + 固定 separators：否则 dict 序变化会让同一份任务算出不同指纹，
      指纹就失去了"同一件事"的判定能力。
    """
    payload = json.dumps(
        {"spec": spec.model_dump(mode="json"), "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 主入口 ────────────────────────────────────────────────
def compile_task(
    spec: TaskSpec,
    params: dict[str, Any] | None = None,
    *,
    chrome_path: str = "",
    headless: bool = True,
    keep_alive: bool = True,
    user_data_dir: str = "",
) -> CompiledTask:
    """编译。这是 Phase 1 的对外主入口。"""
    resolved_params = _coerce_params(spec, params)
    output_model = resolve_output_model(spec.output_model, spec.output_model_version)
    clauses = _render_guardrail_clauses(spec)

    browser_kwargs: dict[str, Any] = {
        "allowed_domains": spec.guardrails.allowed_domains,
        "prohibited_domains": spec.guardrails.prohibited_domains,
        "headless": headless,
        # ★ keep_alive=True 是硬要求，不是优化。
        #   run() 默认会在结束时 kill 浏览器并 await self.close()（service.py:2726）。
        #   任何"停机再续跑"的方案（含护栏回退方案 A）都必须在第二次 run 时
        #   还能拿到同一个活着的浏览器 —— 否则第二次 run 的第一句话就是
        #   "浏览器已经死了"，而那个报错完全指不到 keep_alive。
        "keep_alive": keep_alive,
    }
    if chrome_path:
        # ★ 只在显式指定时才传 executable_path。
        #   传空串会让 browser-use 去找一个名为 "" 的文件并报一个看不懂的错；
        #   不传则它自己走 find_chrome_executable() 探测系统 Chrome。
        browser_kwargs["executable_path"] = chrome_path
    if user_data_dir:
        # ★ 只在显式指定时才传 user_data_dir，理由与 executable_path 同源，
        #   但**更要紧的是语义**，所以单独写一遍：
        #
        #   不传 = 每次一个临时 profile（无状态）→ 默认。
        #   传了 = 复用这个目录里的 cookie（有状态）→ 登录态就是这么来的：
        #         人工跑一次 devtools/login_pdd.py 扫码，之后 run 复用同一目录，
        #         **凭据根本不进 Agent**（不进提示词、不进截图、不需要脱敏代码）。
        #         这比 sensitive_data 占位符替换更硬，见 README 的 ADR-8。
        #
        #   ⚠️ 但传了它就必须传**对**：传一个没登录过的目录不会报错，
        #      只会让 agent 看到登录页 —— 而任务文本里写的是"遇登录页立即停止
        #      并汇报需要人工登录"，于是整个 run 以一句"需要登录"正常结束，
        #      退出码、报告、落库全都正常。**这是一条完全静默的失败**。
        #      所以 CLI 侧配了用前检查（runtime/profile.py）：空路径、目录不存在、
        #      目录没有登录标记，三种情况都会在启动前说出来。
        browser_kwargs["user_data_dir"] = user_data_dir

    agent_kwargs: dict[str, Any] = {
        "use_vision": spec.agent.use_vision,
        "max_actions_per_step": spec.agent.max_actions_per_step,
        "max_failures": spec.agent.max_failures,
    }
    run_kwargs: dict[str, Any] = {
        # ★ max_steps 在 run() 上，不在 Agent 构造函数上。
        #   实测 Agent.__init__ 的签名里没有它 —— 写错地方不会报错，
        #   只会静默地按默认步数跑（于是 max_steps 这个"保险丝"根本没接上）。
        "max_steps": spec.agent.max_steps,
    }

    return CompiledTask(
        spec=spec,
        params=resolved_params,
        task_text=_render_task_text(spec, resolved_params, clauses),
        output_model=output_model,
        browser_kwargs=browser_kwargs,
        agent_kwargs=agent_kwargs,
        run_kwargs=run_kwargs,
        policy=GuardrailPolicy(spec.guardrails),
        fingerprint=compute_fingerprint(spec, resolved_params),
        guardrail_clauses=clauses,
    )
