"""★ 所有对 browser-use 脆弱行为的唯一访问点。

本项目对 browser-use 的依赖不只在公开 API 上，还压在一批【未文档化】的行为上。
散落各处的话，一次 minor 升级就是一场全库考古。集中在这里之后：
  - 升级时只需要改这一个文件
  - `tests/test_compat.py` 是配套的哨兵，升级时主动失败并报出是哪条假设破了

⚠️ 本文件里每一处复刻/封装，都对应 tests/test_compat.py 里的一条断言。
   改这里之前先跑那个测试文件。

为什么 URL 匹配要【复刻】而不是直接调用上游：
  上游有【两套互不兼容】的域名匹配器 ——
    a) SecurityWatchdog._is_url_match    （browser/watchdogs/security_watchdog.py:252-296）
    b) utils.match_url_with_domain_pattern（browser_use/utils.py:563，默认 scheme 是 https）
  两者对同一个 pattern 的判定结果可能不同。我们护栏的 match_url 必须和白名单
  （Layer 0，走 SecurityWatchdog）语义一致，否则会出现"Layer 0 放行、Layer 1 判定不匹配"
  的错位 —— 那种错位不会报错，只会让某条规则静默失活。
"""
from __future__ import annotations

from typing import Any, Iterable, get_args
from urllib.parse import urlparse

# ── 版本契约 ──────────────────────────────────────────────
# 与 pyproject.toml 的精确锁、tests/test_compat.py 的 EXPECTED_VERSION 三处必须一致。
EXPECTED_BROWSER_USE_VERSION = "0.13.10"

# ★ 保留参数名：自定义 action 用了其中任何一个，该参数都不会暴露给 LLM。
#   来源：tools/registry/views.py 的 SpecialActionParameters 字段，
#   在 tools/registry/service.py:278 的 _create_param_model 里被整体剔除。
#   失败是【静默的】：action 照常注册、照常调用，只是 LLM 永远看不到那个参数。
RESERVED_ACTION_PARAM_NAMES = frozenset(
    {
        "context",
        "browser_session",
        "page_url",
        "cdp_client",
        "page_extraction_llm",
        "file_system",
        "available_file_paths",
        "has_sensitive_data",
        "extraction_schema",
    }
)

# ★ 绝不可用作自定义 action 参数名的名字。
#   它不在上面的保留集合里 → 会被当【普通字段原样写进 tool schema 暴露给 LLM】。
#   一份拼多多密码如果进了这里，就会在访问任何站点时都可被填入。这是红线。
FORBIDDEN_ACTION_PARAM_NAMES = frozenset({"sensitive_data"})


def _is_root_domain(domain: str) -> bool:
    """复刻 SecurityWatchdog._is_root_domain（security_watchdog.py:94-109）。

    启发式：只有恰好 1 个点的才算根域（example.com）。
    ★ 这个启发式对 '.com.cn' 这类国家二级域是【不适用】的 —— 上游自己在 docstring 里
      承认了（"For complex cases like country TLDs, users should configure explicitly"）。
      对我们的影响：`pinduoduo.com` 会额外匹配 `www.pinduoduo.com`，这是想要的；
      但 `pinduoduo.com.cn` 不会自动匹配 `www.` 前缀 —— 所以 YAML 里写白名单时，
      要么显式写 `*.pinduoduo.com.cn`，要么两条都列。别指望它自动补。
    """
    if "*" in domain or "://" in domain:
        return False
    return domain.count(".") == 1


def match_url_pattern(url: str, pattern: str) -> bool:
    """URL 是否匹配域名/URL 通配 pattern。

    逐行对照 SecurityWatchdog._is_url_match（security_watchdog.py:252-296）复刻。
    这里的"复刻"是刻意的：护栏的判定必须和 Layer 0 白名单同源，
    否则两条防线会在边界情况上给出不一致的结论，而这种不一致是静默的。

    支持的形态：
        "mms.pinduoduo.com"      精确主机名（大小写不敏感），根域额外匹配 www. 前缀
        "*.pinduoduo.com"        子域 + 主域本身，但【仅限 http/https】
        "https://mms.pinduoduo.com/*"   fnmatch 全 URL
        "https://mms.pinduoduo.com"     前缀匹配
    """
    if not url or not pattern:
        # ★ 空值一律不匹配（fail-closed）。
        #   上游在 _match_domains 那里专门修过一个"fail open"的 bug：
        #   空 page_url（about:blank）曾让所有受限 action 都匹配上。别重蹈覆辙。
        return False

    parsed = urlparse(url)
    host = parsed.hostname or ""      # ★ 用 hostname 而不是 netloc：netloc 带端口和 userinfo，
    scheme = parsed.scheme or ""      #   而 SecurityWatchdog 用的是 hostname。

    full_url_pattern = f"{scheme}://{host}"

    if "*" in pattern:
        if pattern.startswith("*."):
            # *.example.com 匹配子域和主域本身 —— 但只对 http/https 生效。
            # ★ 这一条是"scheme 必须显式"的来源：file:// 或 about: 下的 URL 不会被放行。
            domain_part = pattern[2:]
            if host == domain_part or host.endswith("." + domain_part):
                if scheme in ("http", "https"):
                    return True
        elif pattern.endswith("/*"):
            import fnmatch

            if fnmatch.fnmatch(url, pattern):
                return True
        else:
            import fnmatch

            if fnmatch.fnmatch(full_url_pattern if "://" in pattern else host, pattern):
                return True
    else:
        if "://" in pattern:
            if url.startswith(pattern):
                return True
        else:
            if host.lower() == pattern.lower():
                return True
            if _is_root_domain(pattern) and host.lower() == f"www.{pattern.lower()}":
                return True

    return False


def get_structured_output(history: Any, model: type) -> Any:
    """从 AgentHistoryList 取结构化输出。

    ★ 为什么不用 history.structured_output（那个 property 看着更直观）：
      它依赖私有字段 _output_model_schema，而这个字段【序列化后丢失】。
      所以存过盘再读回时它静默返回 None —— 不报错、不警告，
      表现为"采集成功了但库里没数据"，而且要等到读回历史时才暴露。
      get_structured_output(model) 把 model 当参数传进来，不依赖那个私有字段。

    对应 tests/test_compat.py::test_history_accessors_we_depend_on
    """
    return history.get_structured_output(model)


def get_run_stats(history: Any) -> dict[str, Any]:
    """汇总一次 run 的统计数字。

    ★ total_duration_seconds 是【方法】不是 property（agent/views.py:603 无 @property）。
      本项目所有统计只走这个入口 —— 谁都不许直接点 history.total_duration_seconds，
      因为那样写会拿到 bound method：不报错，直到序列化进 JSON 时才炸，
      而那时栈已经离现场很远了。

    对应 tests/test_compat.py::test_total_duration_seconds_is_a_method_not_a_property
    """
    return {
        "steps": history.number_of_steps(),
        "duration_s": history.total_duration_seconds(),   # ← 调用，不是属性访问
        "urls": list(history.urls()),
        "errors": [e for e in history.errors() if e],
        "final_result": history.final_result(),
    }


def strip_data_uri_prefix(screenshot: str) -> str:
    """剥掉截图可能带的 data URI 前缀，返回裸 base64。

    ★ 反直觉写法：正常情况下浏览器给的 BrowserStateSummary.screenshot 就是【裸 base64】，
      前缀只在 agent/prompts.py:468-475 发给 LLM 时才临时加上。
      所以这里"判断一下"看起来是多余的。

      但成本是一行，收益是：万一哪天上游改了、或者我们从别的路径（比如 Web 层回传、
      或者 LLM 的输出）拿到带前缀的串，不剥就会 base64 解码出一个【损坏的 PNG】——
      而且要到打开报告看图时才发现，那时已经没有任何线索指向这里了。
    """
    if screenshot.startswith("data:"):
        _, _, payload = screenshot.partition(",")
        return payload
    return screenshot


# ── 每步事实的抽取（Phase 3 起）────────────────────────────
# ★ 为什么这两件事必须放在 compat.py：
#   它们读的全是库的【内部形状】—— BrowserStateSummary 的 dataclass 字段名、
#   AgentOutput.action 是"单键 dict 的列表"、AgentHistory.state 是 BrowserStateHistory。
#   这些形状没有一条写在库的公开文档里。散在 recorder/runner 里的话，
#   升级时要满仓库找"哪几行在摸库的内部"，而找到的方式只能是踩一次坑。
#   放这里 + tests/test_compat.py 的哨兵 = 升级时哨兵先红，红了就知道该改哪。
def snapshot_browser_state(
    browser_state: Any, *, indices: Iterable[int] = ()
) -> tuple[dict[str, Any], str | None]:
    """★★★ 必须在 `new_step_callback` 里【当场】调用，返回值才可以留着。

    为什么不能存下 `browser_state` 晚点再读（S2-4 实测，见 docs/spikes.md）：

      · `update_cached_selector_map()` **直接赋值、没有拷贝**（session.py:2494）
        → `browser_state.dom_state.selector_map` 与会话内部缓存
        `_cached_selector_map` 是**同一个 dict 对象**；
      · `reset()` 对它调 `.clear()`（session.py:664）—— **原地清空**；
      · 而 `run()` 在 `keep_alive` 为假时**自己就会 reset**。

      → 默认配置下 `run()` 一返回，所有快照的 selector_map 都是空的。
        失败形态是**静默的**：空 map → 没有规则命中 → 而"没有命中"是**合法结果**
        → 不报错、不告警、测试全绿。**护栏看起来在跑，实际一步都没判。**

    参数 `indices` 是要取文本的元素索引（来自本步的 action）。
    ★ 传进来而不是在这里自己决定：取哪些索引是"这一步想干什么"的知识，
      属于调用方；这里只负责"把它读出来"。

    返回 `(字段 dict, 截图裸 base64 或 None)`。
    ★ 截图单独返回而不是塞进 dict：它很大（几百 KB），塞进去会让
      "结构化的元数据"和"一大坨 base64"混在一个对象里，序列化时很难分开处理。

    ★ 返回值里的 `selector_map_size` 是刻意加的：它让"库把 map 清空了"
      这件事变成一个**数字**。没有它，空 map 和"本来就没元素"长得一样。
    """
    dom = getattr(browser_state, "dom_state", None)
    selector_map = getattr(dom, "selector_map", None) or {}

    texts: dict[int, str] = {}
    for i in indices:
        node = selector_map.get(i)
        if node is None:
            # 索引不在 map 里：元素可能已从 DOM 消失。**不编造文本**，
            # 让它缺席 —— 护栏的 match_element_text 拿到 None 时判不命中，
            # 这是刻意的 fail-safe 方向（见 rules.matches）。
            continue
        try:
            texts[int(i)] = node.get_meaningful_text_for_llm()
        except Exception:  # noqa: BLE001 —— 取文本失败不该让记录器崩掉整个 run
            continue

    shot = getattr(browser_state, "screenshot", None) or None
    fields = {
        "url": str(getattr(browser_state, "url", "") or ""),
        "title": str(getattr(browser_state, "title", "") or ""),
        "element_texts": texts,
        "has_screenshot": shot is not None,
        "selector_map_size": len(selector_map),
    }
    return fields, strip_data_uri_prefix(shot) if shot else None


def extract_step_facts(history_item: Any) -> dict[str, Any]:
    """从一个 `AgentHistory` 里抽出审计要的纯值。

    ★ 返回纯 dict 而不是库的对象：调用方拿到之后，库那边再怎么 reset，
      都不影响这份记录。（和 `snapshot_browser_state` 是同一条纪律的两个面。）

    形状说明（对着 agent/views.py 核过）：
      · `AgentOutput.action` 是 `list`，每个元素 dump 出来是**单键 dict**，
        形如 `{"click": {"index": 3}}` —— 键是动作名，值是参数。
        用 `exclude_unset=True`：否则每个动作都会带上一堆未设置的默认字段，
        日志里全是噪音，而且"LLM 到底传了哪个参数"这个信息会被淹没。
      · `AgentHistory.result` 是 `list[ActionResult]`，与 action 一一对应。
      · `AgentHistory.state` 是 `BrowserStateHistory`（**dataclass，不是 pydantic**），
        有 url / title / screenshot_path。
    """
    facts: dict[str, Any] = {
        "actions": [],
        "results": [],
        "thought": "",
        "url": "",
        "title": "",
        "screenshot_path": None,
    }

    mo = getattr(history_item, "model_output", None)
    if mo is not None:
        parts = [
            str(getattr(mo, "thinking", "") or "").strip(),
            str(getattr(mo, "next_goal", "") or "").strip(),
        ]
        facts["thought"] = " | ".join(p for p in parts if p)

        for act in getattr(mo, "action", None) or []:
            try:
                dumped = act.model_dump(exclude_unset=True)
            except Exception:  # noqa: BLE001
                continue
            for name, params in dumped.items():
                facts["actions"].append(
                    {"name": str(name), "params": params if isinstance(params, dict) else {}}
                )

    for res in getattr(history_item, "result", None) or []:
        extracted = getattr(res, "extracted_content", None)
        facts["results"].append(
            {
                "is_done": bool(getattr(res, "is_done", False)),
                "success": getattr(res, "success", None),
                "error": getattr(res, "error", None),
                # 只留预览：全文在 result.json / 库里，步进日志塞全文会读不动
                "extracted_preview": (str(extracted)[:200] if extracted else None),
            }
        )

    state = getattr(history_item, "state", None)
    if state is not None:
        facts["url"] = str(getattr(state, "url", "") or "")
        facts["title"] = str(getattr(state, "title", "") or "")
        facts["screenshot_path"] = getattr(state, "screenshot_path", None)

    return facts


def page_changing_actions(tools: Any) -> set[str]:
    """库自己标记为「会改变页面」的动作名集合（`RegisteredAction.terminates_sequence`）。

    ★ 为什么用这个标志而不是自己列一张名单：
      `RegisteredAction.terminates_sequence` 的注释原文是
      "known to change the page (e.g. navigate, search, go_back, switch)" ——
      这正是"可能需要预检导航目标"的那一类。自己列名单的话，
      命中的动作名一旦随版本变化（这库在 0.13.x 就把 `go_to_url` 改成了 `navigate`），
      我们那张表会**静默失配**：预检一步都不跑，而日志上看不出任何异常。

    ⚠️ 这个标志**不等于**"导航动作"：`evaluate`（执行任意 JS）也在里面，
       因为它是页面改变类，但它没有 URL 参数。所以调用方拿到集合之后
       仍要按"有没有 url 参数"再分一次 —— 见 interceptor 里的处理。
    """
    registry = getattr(getattr(tools, "registry", None), "registry", None)
    actions = getattr(registry, "actions", None) or {}
    return {name for name, a in actions.items() if getattr(a, "terminates_sequence", False)}


def action_model_fields(agent_or_model: Any) -> set[str]:
    """动作模型上声明了哪些**动作名字段**。

    ★★ 这里踩过一个坑，必须写下来（实测，不是推测）：

      `Agent(tools=...)` 之后 `agent.ActionModel` 是
      `registry.create_action_model()` 的返回值，而它在【动作多于一个】时
      **不是**一个每个动作一个字段的模型，而是一个
      `RootModel[Union[...]]` 包装（`tools/registry/service.py:575-602`），
      它的 `model_fields` **只有 `{'root'}` 一个键**。

      → 直接读 `agent.ActionModel.model_fields` 得到 `{'root'}`，
        于是"里面没有 guard_notice"这个判断会**误报**。
        假警报比不报警更坏：它训练人忽略这个检查，
        而真正该报警的那次就跟着一起被忽略了。

    ★ 所以这里分两种形状处理：
      · 普通动作模型（含单动作时的形态）→ 直接读 `model_fields`；
      · `RootModel` 包装 → 从 `root` 的注解里取出各个 Union 成员，逐个读。
      取不到就返回空集合，语义是"**看不出来**"，不是"没有"。

    ⚠️ 调用方注意：本函数只回答"字段在不在"，**不回答"注入能不能成功"**。
      真正该检查的是端到端往返（造一个实例 → `model_dump(exclude_unset=True)`
      → 动作名正确），那个在 `actions/guard_gate.py` 的 `verify_notice_round_trip`。
      两件事分开：这个函数是诊断信息，那个是启动期门禁。
    """
    model = getattr(agent_or_model, "ActionModel", agent_or_model)
    if model is None:
        return set()

    fields = getattr(model, "model_fields", None)
    if not fields:
        return set()

    names = {k for k in fields if k != "root"}
    if names:
        return names

    # RootModel 包装：root 的注解是 Union[...] / 单个动作模型
    root_field = fields.get("root")
    annotation = getattr(root_field, "annotation", None)
    members = get_args(annotation) or ()
    out: set[str] = set()
    for member in members:
        member_fields = getattr(member, "model_fields", None)
        if member_fields:
            out |= {k for k in member_fields}
    return out


# ── LLM 调用计数（Phase 3 起）──────────────────────────────
# ★ 为什么这三个函数必须在这里：它们摸的全是库的内部形状 ——
#   `agent.AgentOutput` 是运行时造出来的类型、`JudgementResult` 是个 views 里的内部类、
#   `token_cost_service.usage_history` 是库自己的令牌账本。没有一条在公开文档里。
def llm_call_kinds(agent: Any) -> tuple[str, str]:
    """返回 `(step 类调用的输出类型名, judge 类调用的输出类型名)`。

    ★★ 为什么是【问 agent】而不是写死两个字符串：
      step 调用用的 `agent.AgentOutput` 是运行时**动态构造**出来的类型
      （`service.py:786-790` `AgentOutput.type_with_custom_actions(...)`），
      它的名字不是我们的常量，而是库按自己的规则生成的。
      写死 `"AgentOutput"` 的话，库哪天换个命名，我们的分类会**静默全错**——
      总数还是对的（那个是直接数出来的），但"步进/裁判"两项会一起变成 0，
      而 0 看起来像"这次没调用裁判"，不像 bug。

    这里读的是库当时的真名，所以库改名我们跟着改，不需要人去发现。
    """
    # 延迟导入（和本文件里 `import fnmatch` 同样的理由）：compat 被纯逻辑模块
    # （guardrails/rules.py 只用 match_url_pattern）导入，而 agent.views 会拖进
    # 整个 agent 的模型层。一个只需要字符串匹配的单元测试不该为此付出导入代价。
    from browser_use.agent.views import JudgementResult

    step_type = getattr(agent, "AgentOutput", None)
    step_name = getattr(step_type, "__name__", "") if step_type is not None else ""
    return step_name, JudgementResult.__name__


def observed_llm_calls(agent: Any) -> int | None:
    """库自己记的 LLM 调用次数（令牌账本的长度）。

    ★ 这是本项目 CountingLLM 的【独立对照】。
      两边各自维护计数：
        · CountingLLM —— 我们自己包的 ainvoke 包装器，每次调用 +1；
        · 这个账本   —— 库在 `register_llm` 里把 `llm.ainvoke` 换成了它自己的版本
                        （`tokens/service.py:390` `object.__setattr__(llm, 'ainvoke', ...)`），
                        每次拿到 usage 就 append 一条。
      两个数字**独立产生**，所以能互相验证 —— 而单个计数器出错时是看不出来的
      （它只会安静地少一个数）。

    ⚠️ 两者**不保证相等**，这是刻意的、也是有用的：库只在 `result.usage` 为真时才记一条。
       所以"账本更短"意味着有调用没带回 usage（某些 provider/失败路径会这样），
       而不是"我们数多了"。runner 把两个数都记下来，差额进 run.json，
       让"哪一类调用不带 usage"变成一个可观察的事实而不是一个假设。

    返回 None 表示这个版本没有可用的账本 —— 那时只有我们自己的计数，
    run.json 里那一格会是 null（**显式缺席**，不是 0；0 会被误读成"库说是零次调用"）。
    """
    service = getattr(agent, "token_cost_service", None)
    if service is None:
        return None
    history = getattr(service, "usage_history", None)
    if history is None:
        return None
    try:
        return len(history)
    except TypeError:
        return None


def observed_token_totals(agent: Any) -> tuple[int, int] | None:
    """库账本里的 (prompt_tokens, completion_tokens) 合计。

    ★ 直接累加 `usage_history` 里每条 entry 的 usage，**不调** `get_usage_tokens_for_model()`。
      后者要先加载价格表（`_load_pricing_data`），而那个会**联网抓定价**——
      一个纯统计动作不应该有网络副作用，尤其 CI 里会因此变成一个偶发失败源。
      我们只要 token 数，不需要钱。
    """
    service = getattr(agent, "token_cost_service", None)
    history = getattr(service, "usage_history", None)
    if not history:
        return None
    prompt = completion = 0
    for entry in history:
        usage = getattr(entry, "usage", None)
        if usage is None:
            continue
        prompt += int(getattr(usage, "prompt_tokens", 0) or 0)
        completion += int(getattr(usage, "completion_tokens", 0) or 0)
    return prompt, completion


def patched_ainvoke_target(llm: Any) -> Any:
    """返回库在 `register_llm` 里替换掉的 `ainvoke`（即我们自己的那个），没有则 None。

    ★ 用途只有一个：诊断。它回答"我们的包装器到底还在调用链上吗"——
      如果库哪天改成**替换整个 llm 对象**（而不是只换它的 ainvoke），
      我们的计数会变成 0，而 0 是一个看起来很正常的值。
      这个函数让那种改动可以被一条断言直接检出，而不是靠人去注意计数变 0 了。

      判断依据：库的包装器是个闭包（函数名 `tracked_ainvoke`），
      而我们的包装器是绑定方法。所以"是个函数但不是方法"就说明被换过。
    """
    fn = getattr(llm, "ainvoke", None)
    if fn is None:
        return None
    if getattr(fn, "__name__", "") == "tracked_ainvoke":
        # 已被库替换 → 我们的原方法在闭包的第 1 个自由变量里（original_ainvoke）。
        cells = getattr(fn, "__closure__", None) or ()
        for cell in cells:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value) and getattr(value, "__self__", None) is llm:
                return value
        return None
    return fn
