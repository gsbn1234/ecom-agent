"""这次 run **实际看到的**登录态 —— 用来分辨「零行」那两种完全不同的原因。

★★ 为什么需要它（这是 Phase 6 真站点首跑撞出来的，不是推演）：

  一次真站点 run 跑完、`parse_status=empty`、零行，可能是两件毫无关系的事：

    a) 页面正常，只是**没有数据**（新店还没上架商品 / 搜索无结果）；
    b) 登录态没生效，agent 落在**登录页**上，于是按任务文本规规矩矩地停下汇报。

  而这两件事在产物里**长得一模一样**：`status=completed`、`parse_status=empty`、
  退出码 0、报告齐全、截图也都有。当时唯一能分辨的办法，是**读 LLM 写的那句
  中文 note，或者人肉去看截图** —— 也就是说结论靠的是模型的措辞，不是机制。
  换个措辞、或者换个不爱写 note 的模型，就又回到"看不出为什么"。

  ⚠️ 而且那次是**空店帮我们暴露了它**。店里有 200 件商品时，某次 cookie 过期
  会给出完全一样的产物，还更难起疑（"上次还好好的"）。所以这个字段的价值
  与店里有货没货无关。

★ 记录的是**观察到的**事实，不是**配置的**事实 —— 这个区别是全部意义所在：

    把"配了哪个 profile"写进产物是**没用**的。cookie 过期时它照样老老实实写着
    那个配置好的路径，而 run 照样停在登录页。它能回答"我们配了哪个 profile"，
    回答不了"这次到底登进去没有"。有用的只有"这一页到底是不是登录页"。

★ 判定逻辑与 `devtools/login_pdd.py` **共用同一份**（它原来住在那儿）。
  写登录标记用的判据，和 run 里记录登录态用的判据，必须是同一个 ——
  否则会出现"标记说就绪、run 说落在登录页"这种自己跟自己矛盾的产物，
  而那个矛盾的排查成本远高于它挡住的问题。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── 判定的取值 ────────────────────────────────────────────
LOGGED_IN = "logged_in"
LOGIN_PAGE = "login_page"
UNKNOWN = "unknown"
"""探过了，但看不清 —— **不是**"没登录"。两者混为一谈会逼着判定在
看不清的画面上选一个，而选错的方向恰好最坏（把"没登录"报成"登录了"）。"""

NOT_PROBED = ""
"""压根没探。与 `UNKNOWN` 必须分开：`""` 是"这个任务不需要登录态/没导航到起点"，
`UNKNOWN` 是"探了但拿不准"。压成同一个值，报告就没法说清它到底是哪种沉默。"""

# ── 判定词汇 ──────────────────────────────────────────────
LOGIN_PAGE_HINTS = ("扫码登录", "账号登录", "密码登录", "短信登录", "请登录", "登录/注册")
LOGGED_IN_HINTS = (
    "商品管理", "订单管理", "发货管理", "售后管理", "数据中心", "店铺", "商家后台",
)

# 判不出来时重看几次（见 probe）。★ 只在**判不出来**时才等 —— 判出来了就立刻走，
# 不拿所有人的时间去买一个少数情况。
SETTLE_TRIES = 3
SETTLE_S = 1.0


def classify(url: str, dom_text: str) -> tuple[str, str]:
    """返回 (判定, 依据)。判定 ∈ {logged_in, login_page, unknown}。

    ★ 依据一并返回，因为它要**原样给人看**：判定错了的时候，唯一能让人快速分清
      "是判错了还是真的没登录"的就是它（命中了哪些词、看的是哪个 URL）。
    ★ 顺序有意为之：**URL 优先于页面文字**。落在 `passport`/`/login` 上时，
      页面上残留的"商品管理"之类字样一律不作数 —— 那种页面正是跳转中间态。
    """
    low = url.lower()
    if "/login" in low or "passport" in low:
        return LOGIN_PAGE, f"URL 落在登录路径上：{url}"

    logged = [w for w in LOGGED_IN_HINTS if w in dom_text]
    if logged:
        return LOGGED_IN, f"页面上出现了登录后才有的导航：{'、'.join(logged[:3])} @ {url}"

    login_ish = [w for w in LOGIN_PAGE_HINTS if w in dom_text]
    if login_ish:
        return LOGIN_PAGE, f"页面上出现登录字样：{'、'.join(login_ish[:3])} @ {url}"

    return UNKNOWN, f"URL 与页面文本都不足以判定（{url}，正文 {len(dom_text)} 字符）"


@dataclass(frozen=True)
class LoginState:
    """一次探测的结果。`verdict` 是给机器看的，`reason` 是给人看的。"""

    verdict: str = NOT_PROBED
    reason: str = ""
    url: str = ""

    @property
    def is_login_page(self) -> bool:
        """★ 只有这一个方向值得让代码做判断：确认落在登录页 = 这个 run 注定零行。"""
        return self.verdict == LOGIN_PAGE

    def describe(self) -> str:
        if self.verdict == NOT_PROBED:
            return "未探测（任务不需要登录态，或没有导航到起点）"
        return f"{self.verdict} —— {self.reason}"


async def read_page_state(session) -> tuple[str, str]:
    """取一次 (url, DOM 文本)。★ 只读本机 CDP，不产生对目标站点的新请求。"""
    state = await session.get_browser_state_summary(include_screenshot=False)
    dom_text = ""
    if state.dom_state is not None:
        dom_text = state.dom_state.llm_representation() or ""
    return (state.url or ""), dom_text


async def probe(session, *, tries: int = SETTLE_TRIES, gap_s: float = SETTLE_S) -> LoginState:
    """导航到起点之后立刻判一次：这一页到底是不是登录页。

    ★ 判不出来才重看（默认最多 3 次、每次隔 1s），因为跳转中间态会短暂地
      两边都不像。判出来了立刻返回 —— **不做无条件等待**。

    ★★ 这个函数**绝不允许把 run 弄挂**：它是诊断，不是运行的一部分。
      会话读不到（窗口关了 / CDP 断了）时返回 unknown 并把异常写进依据，
      而不是抛出去 —— 一次探测失败不该让一次本来能跑完的 run 崩掉。
    """
    last = LoginState(UNKNOWN, "还没开始探")
    for attempt in range(1, max(1, tries) + 1):
        try:
            url, dom_text = await read_page_state(session)
        except Exception as exc:  # noqa: BLE001
            return LoginState(
                UNKNOWN,
                f"读取页面状态失败（窗口关了 / 会话断了？）：{type(exc).__name__}: {exc}",
            )
        verdict, why = classify(url, dom_text)
        last = LoginState(verdict, why, url)
        if verdict != UNKNOWN:
            return last
        if attempt < tries:
            await asyncio.sleep(gap_s)

    return last
