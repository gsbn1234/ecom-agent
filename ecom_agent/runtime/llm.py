"""LLM 的构造与【调用计数】。

★★ 为什么计数要包一层，而不是在 run 结束后数 `history` 的步数：

  一个 run 里 LLM 被调用的次数 **不等于** 步数。已核实的三类额外调用：

    · **judge**：run 结束后库额外发一次评估调用（`agent/service.py:1620`，N+1 的来源）；
    · **extract**：`extract` 动作内部调 `page_extraction_llm`
      （`tools/service.py:1197` 和 `1276`，两处 —— 带 schema 的和不带 schema 的）；
    · **summary / compaction**：历史压缩时调 `compaction_llm`（`service.py:1168`）。

  只记步数会把成本算少，而"算少"不会报错 —— 它只会让预算判断一直偏乐观。
  这是 S4 探路时顺带发现的（见 docs/spikes.md）。

★★ 为什么包【一个对象】就能覆盖全部（这条是核实过的，不是推理）：

  `Agent.__init__` 里三个 LLM 槽位在未显式传入时**全部回落到同一个 llm 对象**：

      service.py:253-254   if page_extraction_llm is None: page_extraction_llm = llm
      service.py:255-256   if judge_llm          is None: judge_llm          = llm
      service.py:1168      compaction_llm = settings.compaction_llm or page_extraction_llm or llm

  → 所以 `llm=` 传进去的那个对象是**所有** LLM 流量的必经之路，包它一个就够了。

  ⚠️ 唯一的例外是 `fallback_llm`：它是独立对象（`service.py:2023` 单独注册），
     走它的请求不经过我们。本项目的 config 不配 fallback_llm，所以不适用 ——
     但**写在这里**，因为将来有人加上它时会静默少算，而少算表现出来的样子
     就是"这个月怎么比上个月便宜"。

★★ 还有一个已核实的重要事实：库自己也在这条路上做手脚。
  `token_cost_service.register_llm()` 会把 `llm.ainvoke` **整个替换掉**
  （`tokens/service.py:390` `object.__setattr__(llm, 'ainvoke', tracked_ainvoke)`），
  换成它自己的包装版本，每次拿到 usage 就追加一条账本记录。

  这带来两件事：
    1. 我们的 `ainvoke` 是它闭包捕获的 `original_ainvoke` —— **仍然会被调用**，
       所以计数不受影响。（顺序上也安全：我们在包完之后才把对象交给 Agent。）
    2. 它那个账本正好成了我们的**独立对照**（见 `compat.observed_llm_calls`）。
       两个数字各自产生、互不依赖 —— 单个计数器出错时是看不出来的，
       它只会安静地少一个数；两个计数器对不上才会暴露。
"""
from __future__ import annotations

import logging
from typing import Any

from ecom_agent.config import DEEPSEEK_API_KEY
from ecom_agent.observability.models import LlmUsage

logger = logging.getLogger(__name__)


class CountingLLM:
    """透明代理：转调内层 LLM，同时数调用次数与 token。

    ★「透明」是被测过的，不是形容词：`__getattr__` 把一切没定义的属性转给内层，
      所以 `llm.model` / `llm.provider` 这些库要读的字段照常工作
      （`register_llm` 第 363-364 行就要读这两个）。
      没有这层委托的话，库会在 `llm.provider` 那里抛 AttributeError ——
      而那个报错出现在 Agent 构造期间，完全指不到"你包了一层"。
    """

    def __init__(self, inner: Any, *, usage: LlmUsage | None = None) -> None:
        # ★ 用 __dict__ 直接写：__getattr__ 里也要读这个键，
        #   走 self._inner = ... 的话，若 __init__ 中途失败，
        #   __getattr__ 会因为找不到 _inner 再去读 _inner —— 无限递归。
        self.__dict__["_inner"] = inner
        self.__dict__["usage"] = usage if usage is not None else LlmUsage()

    # ── 计数点 ────────────────────────────────────────────
    async def ainvoke(
        self, messages: Any, output_format: Any = None, **kwargs: Any
    ) -> Any:
        """数一次调用，然后转调内层。

        ★ 参数顺序必须是 `(messages, output_format, **kwargs)` 且 output_format
          可以被**位置**传入：库的包装器是
          `original_ainvoke(messages, output_format, **kwargs)`（tokens/service.py:373），
          位置传参。写成 keyword-only 的话它在运行期炸，而栈会指向 tokens 服务 ——
          离真正的起因（签名不兼容）很远。
        """
        usage: LlmUsage = self.__dict__["usage"]
        kind = getattr(output_format, "__name__", None) or "none"

        # ★ 先记后调，且异常也留在计数里：一次失败的请求也可能已被计费
        #   （超时、5xx）。宁可略微高估，不可低估。
        usage.total_calls += 1
        usage.by_format[kind] = usage.by_format.get(kind, 0) + 1

        try:
            result = await self.__dict__["_inner"].ainvoke(messages, output_format, **kwargs)
        except BaseException:
            # ★ BaseException 而不是 Exception：CancelledError 也要记。
            #   被取消的请求同样可能已经发出去并计费了。
            usage.failed_calls += 1
            raise

        self._absorb_tokens(result)
        return result

    def _absorb_tokens(self, result: Any) -> None:
        """把这一次的 token 用量累加进去。

        ★ `usage` 可能是 None（库的 views 里它是必填字段但可以为 None）——
          所以三元表达式不写成 `result.usage.prompt_tokens` 那种链式访问。
          某些 provider 在失败或流式场景下就是不返回 usage，那不是错误。
        """
        u = getattr(result, "usage", None)
        if u is None:
            return
        usage: LlmUsage = self.__dict__["usage"]
        usage.prompt_tokens += int(getattr(u, "prompt_tokens", 0) or 0)
        usage.completion_tokens += int(getattr(u, "completion_tokens", 0) or 0)

    # ── 透明性 ────────────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def __repr__(self) -> str:
        inner = self.__dict__.get("_inner")
        return f"<CountingLLM {inner!r}>"


# ── 构造真实 LLM ──────────────────────────────────────────
def build_llm(
    *,
    model: str = "deepseek-chat",
    api_key: str | None = None,
    temperature: float | None = None,
) -> Any:
    """构造 DeepSeek 聊天模型。

    ★ 必须**显式**传 api_key：`ChatDeepSeek` 从不读 `DEEPSEEK_API_KEY`
      （`llm/deepseek/chat.py:28-58`）。它的 `api_key` 默认是 None →
      透传给 AsyncOpenAI → 回退去读 `OPENAI_API_KEY` → 报一个
      "缺少 OPENAI_API_KEY" 的错，而你手上明明是 DeepSeek 的 key。
      那个报错把人往完全错误的方向带，所以这里显式传，并且**没有 key 就直接拒绝**。
    """
    key = api_key if api_key is not None else DEEPSEEK_API_KEY
    if not key:
        raise RuntimeError(
            "缺少 DEEPSEEK_API_KEY：真调 LLM 必须显式提供 key。"
            "（CI 与单测请走离线路径：注入 FakeLLM，不要走 build_llm。）"
        )

    from browser_use.llm.deepseek.chat import ChatDeepSeek

    kwargs: dict[str, Any] = {"model": model, "api_key": key}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatDeepSeek(**kwargs)


def counted(llm: Any, *, usage: LlmUsage | None = None) -> CountingLLM:
    """把任意 LLM 包成带计数的。★ 已经是 CountingLLM 就原样返回，避免套两层
    （套两层会让计数翻倍，而"翻倍"看起来像"这个任务的调用确实很多"）。"""
    if isinstance(llm, CountingLLM):
        return llm
    return CountingLLM(llm, usage=usage)
