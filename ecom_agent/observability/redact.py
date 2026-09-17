"""落盘前的脱敏 —— 审计日志的最后一道过滤。

★ 为什么自研，不复用 `browser_use.utils.redact_sensitive_string`：
  1. **定位不同。** 那个函数的服务对象是"发给 LLM 的消息"，目标是别把密码喂给模型；
     我们洗的是**审计日志**，要求严得多：cookie、会话标识、订单号都得盖。
     一个"够用就行"的实现放在错误的用途上，最危险的地方在于它看起来在工作。
  2. **它是库的内部工具**，签名和语义随版本变；审计日志是长期资产，不能绑在会变的上游上。
  3. **自研版能叠正则形态脱敏**（手机号 / 邮箱 / 身份证 / cookie 值），
     库的版本只做精确值替换 —— 它对"LLM 从页面上抄下来的一个手机号"无能为力，
     而那个手机号恰恰会出现在模型的思考文本里。

★ 调用时机：**在序列化之前**，不是写完再洗。
  写完再洗意味着盘上曾经存在过一份未脱敏的内容，而"曾经存在过"在
  (a) 崩溃中断、(b) 别的进程正在 tail、(c) 文件系统的写前日志 三种情况下都会留下来。
  这里只提供"进得来出不去"的入口：任何要落盘的结构都必须先过 `obj()`。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ── 字面值脱敏的最小长度 ──────────────────────────────────
# ★ 为什么设下限并且【直接报错】而不是跳过：
#   一个 1~2 字的脱敏词（比如店铺名"小店"）会把日志里所有出现"小店"的地方替换掉 ——
#   包括"商品小店"这种无关文本，于是日志变得不可读。
#   而"不可读"是静默的：脱敏照常"成功"，只是审计价值没了。
#   报错会让配置的人立刻知道，跳过会让他永远不知道。
MIN_LITERAL_LEN = 3

# 字面值用的占位符（不区分来源，因为来源可能有多个，标错比不标更误导）
LITERAL_PLACEHOLDER = "<REDACTED>"


def _p(kind: str, pattern: str, group: int = 0) -> tuple[str, re.Pattern[str], int]:
    """把一个正则包成三元组。group=0 表示整段匹配都替换掉。"""
    return kind, re.compile(pattern), group


# ── 正则形态表 ────────────────────────────────────────────
# 顺序 = 应用顺序。带捕获组的规则只替换【捕获组】，这样
# `sessionid=abc123` 会变成 `sessionid=<REDACTED:cookie>` —— 键还在，值没了。
# 键必须留着：审计时"这里有 cookie"和"这里有 sessionid 但没有别的"是两条不同信息。
#
# ★★ 这里【刻意没有】"长数字串"这条规则，这是一个重要的设计取舍：
#    商品 ID（pdd 的 goods_id）本身就是一串长数字。
#    一条笼统的"≥N 位数字一律盖掉"会把**任务真正要采集的数据**一起毁掉，
#    而且毁得很安静 —— 库里 goods_id 变成 <REDACTED>，报告看起来"脱敏很到位"。
#    反过来，订单号**没有**跨平台通用的形态，正则认不出来。
#    所以订单号的正确做法是写进 YAML 的 `observability.redact_extra`（精确值），
#    而不是指望这里猜。**这条限制写进 docs/guardrail_design.md 的
#    「落盘脱敏的失效边界」一节，以及 tests/test_sanity_and_pii.py。**
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    # 邮箱：形态稳定，可以放心用宽正则
    _p("email", r"[\w.+-]+@[\w-]+\.[\w.-]*[\w]"),
    # 中国大陆手机号：两边加数字边界，避免把 19 位订单号里的中间 11 位当成手机号
    _p("phone", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 身份证：17 位数字 + 校验位（数字或 X）
    _p("id_card", r"(?<!\d)\d{17}[\dXx](?!\d)"),
    # 会话/凭证类：键名保留，只盖值
    _p("token", r"(?i)\b(?:bearer|token|api[_-]?key|access[_-]?key|secret)\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})", 1),
    # ★★ 授权头的「方案 + 空白 + 凭证」形态（2026-09-17 补，补的是上一条的缺口）：
    #    上一条要求 `[:=]` 出现在关键字**之后**，而规范头部
    #    `Authorization: Bearer <token>` 里冒号在 `Bearer` **之前** →
    #    `bearer` 分支对它的规范形态一个字都不盖；而 `token:` / `api_key=` 是好的，
    #    所以它**看起来在工作** —— 正是本模块 docstring 警告过的那种失效。
    #
    # ★ 为什么锚在头名上，而不是写成笼统的「bearer + 空白 + 值」：
    #   后者会误伤真实散文。本机实测（不是推演）：
    #       "error: Bearer authentication is required"
    #     → "error: Bearer <REDACTED:token> is required"   把 authentication 当凭证盖了
    #   `authentication` 有 14 个字符、又全在字符类里，所以它**必然**命中。
    #   锚在 `authorization:` 之后，副作用面几乎为零，而本次要修的形态**恰好**在那里。
    #   代价：不在头部、单说一句 `Bearer <值>` 的地方（比如 JSON 体里的字段）仍不盖 ——
    #   那种形态等真见到再补，或者走 YAML 的 redact_extra，不在这里猜。
    _p("token", r"(?i)\b(?:proxy-)?authorization\s*:\s*bearer\s+[\"']?([A-Za-z0-9._\-]{12,})", 1),
    _p(
        "cookie",
        r"(?i)\b(?:sessionid|session_id|sess_id|csrf[_-]?token|_csrf|antiforgery)\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{8,})",
        1,
    ),
    # Set-Cookie 头：值到分号为止
    _p("cookie", r"(?i)set-cookie\s*:\s*([^;\s]+)", 1),
]


class Redactor:
    """一次 run 的脱敏器。

    ★ 为什么是【实例】而不是一组模块级函数：
      字面值词表按 run 变化（来自 YAML 的 redact_extra），而"长值优先"要求词表被排序。
      每次调用都排序一遍是浪费，更重要的是：把"这份词表排过序"这个前提
      分散到调用点上，早晚会有一处忘了排 —— 而忘了排的后果是
      `abcdef` 没被替换掉（因为 `abc` 先被换走了），**静默漏一个真值**。

    ★ 累积 counts 而不是只做替换：
      "本次脱敏了 N 处"是**脱敏真的跑过**的证据。一个 counts 全为 0 的 run
      如果页面上明明有 cookie，那就是词表或规则失效的信号 ——
      没有这个计数，"什么都没盖住"和"本来就没有可盖的"长得一模一样。
    """

    def __init__(self, literals: Iterable[str] = (), *, extra_patterns: Iterable[str] = ()) -> None:
        cleaned: list[str] = []
        for raw in literals:
            if raw is None:
                continue
            s = str(raw)
            if not s:
                continue
            if len(s) < MIN_LITERAL_LEN:
                raise ValueError(
                    f"脱敏词 {s!r} 太短（{len(s)} < {MIN_LITERAL_LEN}）："
                    f"它会替换掉日志里所有出现这两个字的地方，把审计价值一起抹掉。"
                    f"请给完整的店铺名/订单号，而不是它的片段。"
                )
            cleaned.append(s)

        # ★ 去重后按长度【降序】—— 长值优先替换。
        #   反过来的话：先换掉 "abc"，那么 "abcdef" 里剩下的 "def" 就再也匹配不上，
        #   盘上留下一段半截的真值。而且它看起来像"替换成功了一部分"，不像 bug。
        self._literals: list[str] = sorted(set(cleaned), key=len, reverse=True)

        # 调用方额外给的正则（YAML 里没有这个入口，留给脚本/测试用）
        self._extra: list[re.Pattern[str]] = [re.compile(p) for p in extra_patterns]

        self.counts: dict[str, int] = {}

    # ── 主入口 ────────────────────────────────────────────
    def text(self, s: str) -> str:
        """脱敏一个字符串。非字符串原样返回（None 也是）。"""
        if not s:
            return s
        out = s
        for lit in self._literals:
            if lit in out:
                n = out.count(lit)
                out = out.replace(lit, LITERAL_PLACEHOLDER)
                self._bump("literal", n)
        for kind, rx, group in _PATTERNS:
            out, n = self._sub_group(out, rx, group, kind)
            if n:
                self._bump(kind, n)
        for rx in self._extra:
            out, n = self._sub_group(out, rx, 0, "extra")
            if n:
                self._bump("extra", n)
        return out

    def obj(self, value: Any) -> Any:
        """递归脱敏一个可 JSON 化结构，返回**新对象**（不改入参）。

        ★ 为什么不改入参：调用方常常还要用原值（比如把同一份 params 交给
          ActionResult），就地改写会让"记录"这个动作产生副作用 ——
          而观测代码产生了副作用，是排查时最难想得到的一类 bug。

        ★ 为什么【不】脱敏 dict 的键：
          这些记录的键全部来自我们自己的 schema 和 library 的 action 参数模型
          （index / url / text / is_done …），没有一个是页面或 LLM 自由生成的。
          给键做替换的代价是可能撞名（两个不同的键被盖成同一个 → 静默丢一个字段），
          收益是零。这是一个"没有收益、只有风险"的操作，所以不做。
        """
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value

    # ── 内部 ──────────────────────────────────────────────
    @staticmethod
    def _sub_group(text: str, rx: re.Pattern[str], group: int, kind: str) -> tuple[str, int]:
        """只替换第 group 组，保留匹配的其余部分。

        用 sub + 回调实现，因为要"保留命中的上下文、只换掉值"。
        """
        if group == 0:
            new, n = rx.subn(f"<REDACTED:{kind}>", text)
            return new, n

        count = 0

        def repl(m: re.Match[str]) -> str:
            nonlocal count
            count += 1
            # ★ 用 match 对象给的【精确偏移】定位捕获组，不要用 str.rfind 去找值。
            #   rfind 在"值内部重复"时会取到最后一个出现位置（`token=aXaXaX…`），
            #   于是切错位置 —— 留下一段半截的真值，而且看起来像替换成功了。
            #   m.start(group) 是相对整个字符串的，减去 m.start(0) 才是段内偏移。
            gs = m.start(group) - m.start(0)
            ge = m.end(group) - m.start(0)
            whole = m.group(0)
            return whole[:gs] + f"<REDACTED:{kind}>" + whole[ge:]

        return rx.sub(repl, text), count

    def _bump(self, kind: str, n: int) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + n

    # ── 构造 ──────────────────────────────────────────────
    @classmethod
    def from_spec(cls, spec: Any) -> "Redactor":
        """从 TaskSpec.observability 构造。"""
        return cls(getattr(spec, "redact_extra", ()) or ())

    def summary(self) -> str:
        """给人看的一行：这次脱敏盖掉了什么。"""
        if not self.counts:
            return "本次未发生脱敏（没有匹配到任何字面值或敏感形态）"
        parts = [f"{k}={v}" for k, v in sorted(self.counts.items())]
        return "本次脱敏：" + "，".join(parts)
