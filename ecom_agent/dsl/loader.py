"""YAML → TaskSpec。

★ 这个模块唯一的职责是【把错误说清楚】。
  解析本身是 pydantic 干的活，这里做的是：让每一条报错都能直接指到
  「哪个文件的哪个字段」，而不是甩一个 ValidationError 的 repr 出来。

  为什么值得单独一个模块：任务模板是给人写的（也是要进 code review 的）。
  写 YAML 的人犯错是常态，报错质量直接决定了这个 DSL 好不好用 ——
  一份"报错看不懂"的 DSL，最后会退化回"大家直接写 prompt"。
"""
from __future__ import annotations

import difflib
from pathlib import Path

import yaml
from pydantic import ValidationError

from ecom_agent.dsl.models import (
    AgentSpec,
    ObservabilitySpec,
    PaginationSpec,
    ParamSpec,
    RetrySpec,
    TaskSpec,
)
from ecom_agent.guardrails.rules import GuardrailRule, GuardrailSpec


class TaskLoadError(ValueError):
    """任务模板加载失败。消息里一定带来源（文件路径或 <string>）。"""


def load_task_str(text: str, source: str = "<string>") -> TaskSpec:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # ★ YAML 语法错误必须带上行号 —— 大段 YAML 里没有行号等于让人自己数。
        mark = getattr(e, "problem_mark", None)
        where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列" if mark else "位置未知"
        raise TaskLoadError(f"{source}: YAML 语法错误（{where}）：{e}") from e

    if raw is None:
        raise TaskLoadError(f"{source}: 文件是空的")
    if not isinstance(raw, dict):
        raise TaskLoadError(f"{source}: 顶层必须是映射（key: value），实得 {type(raw).__name__}")

    try:
        return TaskSpec.model_validate(raw)
    except ValidationError as e:
        raise TaskLoadError(f"{source}: 任务定义不合法\n{_format_errors(e)}") from e


# 拼错字段名时，去这些模型里找相近的候选。
# ★ 为什么要跨模型收集：报错的 loc 可能落在任意嵌套层（guardrails.rules[0].match_action），
#   而使用者心里想的永远是"我要写的那个字段名"，不是"它属于哪个模型"。
#   从使用者的视角出发去找候选，比按 loc 精确路由到某个模型更符合实际用法。
_CANDIDATE_MODELS = (
    TaskSpec,
    GuardrailSpec,
    GuardrailRule,
    PaginationSpec,
    RetrySpec,
    ObservabilitySpec,
    AgentSpec,
    ParamSpec,
)


def _suggest(name: str) -> str:
    """"是不是想写 X？" —— 对拼错字段名的场景，这一句省掉一次查文档。"""
    known: list[str] = []
    for m in _CANDIDATE_MODELS:
        known.extend(m.model_fields)
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    return f"（是不是想写 {close[0]!r}？）" if close else ""


def _format_errors(e: ValidationError) -> str:
    """把 pydantic 的报错整理成「字段路径 → 原因」。

    ★ 保留 loc（字段路径）是重点：没有它，"Input should be a valid integer"
      这种消息在一份 80 行的 YAML 里毫无定位价值。
      路径用 . 连接，和 YAML 里的嵌套结构对应，可以直接照着想。

    ★ 对拼错的字段名额外给一个"是不是想写 X"。
      这不是锦上添花：任务模板是给人写的，而这份 DSL 的价值主张就是"可读可 review"。
      报错看不懂的 DSL，最后一定会退化回"大家直接写 prompt"——
      那时任务定义重新变得不可 review，整个设计目标就落空了。
    """
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<顶层>"
        msg = err["msg"]
        if err["type"] == "extra_forbidden" and err["loc"]:
            msg += _suggest(str(err["loc"][-1]))
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def load_task(path: str | Path) -> TaskSpec:
    p = Path(path)
    if not p.is_file():
        # ★ 报错时列出同目录下有哪些模板 —— "文件不存在"最常见的原因是名字记错了，
        #   直接把候选列出来比让人去 ls 一次有用得多。
        siblings = sorted(x.name for x in p.parent.glob("*.yaml")) if p.parent.is_dir() else []
        hint = f"该目录下现有的模板：{siblings}" if siblings else "该目录下没有任何 .yaml"
        raise TaskLoadError(f"任务模板不存在：{p}\n{hint}")

    # ★ 显式 encoding="utf-8"：任务模板里有中文（状态名、元素文本、护栏说明）。
    #   不写的话在 Windows 上会按 GBK 读，报一个 UnicodeDecodeError ——
    #   而那个报错看起来像是"文件损坏了"，不是"编码没指定"。
    return load_task_str(p.read_text(encoding="utf-8"), source=str(p))
