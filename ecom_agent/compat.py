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

from typing import Any
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
