"""runtime —— 把 CompiledTask 变成一次真实运行。

三个文件，职责刻意分开：

    browser.py   浏览器会话的构造与【保证关闭】
    llm.py       真 LLM 的构造 + 调用计数（CountingLLM）
    runner.py    编排：编译产物 → 浏览器 → Agent → 产物落盘 → 落库

★ 这里（连同 `actions/`）是**唯一 import browser_use 顶层类**的地方。
  下面的层（dsl / guardrails/rules+policy / observability/redact / sites）
  一行都不 import 它 —— 那正是它们能零浏览器、零 token 被穷举测试的原因。

★ 本模块**刻意不在 `__init__` 里 re-export** runner 的入口。
  理由：`from ecom_agent.runtime import run_task` 会让"只想 import 一个
  纯函数"的测试被迫走一遍 runtime 的导入链。要用什么就显式 import 那个模块。
"""
