"""持久登录 profile 的「用前检查」与「登录标记」。

★ 这个文件存在的唯一理由是一条**完全静默**的失败：

  给 BrowserSession 传一个没登录过的 user_data_dir，不会报任何错。
  而任务文本里写着「若出现登录页，立刻停止并调用 done 汇报'需要人工登录'」，
  于是 agent 规规矩矩地停下、run 正常结束、退出码 0、报告和落库都齐全 ——
  只是**零行数据**。人会先去怀疑风控、怀疑 cookie 过期、怀疑选择器改版，
  而真正的原因（登录用的目录和跑用的目录不是同一个）没有任何地方说过话。

  同样的道理，`.env` 里没配 profile 而任务 requires_login 时也一样：
  不是崩，是"白跑一次"，且看起来一切正常。

  所以这里提供**会在启动前说话**的检查。★ 它是**提示（advisory）不是门禁**：
  cookie 会过期，而"标记文件存在"只说明**那一刻**复核过 —— 拿它去阻止运行
  会在最不该拦的时候拦人（刚过期、急着看报告）。它的职责只有一个：
  把「跑一次白跑」变成「先说一句」。

★ 标记文件里**没有 cookie**，只有站点名、登录后的 URL、时间戳。
  cookie 本身在 profile 目录里（那是 Chrome 的格式），整个目录已在 .gitignore。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ecom_agent.config import DEFAULT_PROFILE_DIR

logger = logging.getLogger(__name__)

# 前缀点号：它会躺在 Chrome 的 profile 目录里，用点号开头表明"这是外来的记账文件"。
PROFILE_MARKER_NAME = ".ecom_agent_profile.json"

# 写入方只有一种，且必须验过才写 —— 见 devtools/login_pdd.py：
# 扫完码当场判定"已登录"不算数（那一刻的页面可能还没跳转完），
# 要**关掉浏览器、用同一个目录开一个全新会话**再看一次。
MARK_VERIFIED_BY = "fresh_session"


class ProfileMarkError(ValueError):
    """标记文件存在、但读不出我们需要的内容。"""


@dataclass(frozen=True)
class ProfileMark:
    """一次**被复核过**的登录留下的记录。"""

    site: str
    logged_in_url: str
    verified_at: str
    verified_by: str = MARK_VERIFIED_BY

    def describe(self) -> str:
        return (
            f"{self.site} @ {self.verified_at}"
            f"（{self.verified_by} 复核；当时停在 {self.logged_in_url}）"
        )


def marker_path(profile_dir: str | Path) -> Path:
    return Path(profile_dir) / PROFILE_MARKER_NAME


def read_mark(profile_dir: str | Path) -> ProfileMark | None:
    """读到就返回；文件不存在返回 None；**存在但读不进去 → 抛 ProfileMarkError**。

    ★ 为什么"坏标记"要抛，而不是当成"没有标记"：
      两者的处置完全不同 ——「没有标记」是"去跑一次登录脚本"，
      「坏标记」是"这个目录被人动过或写坏了，先看看它到底是什么"。
      归成同一句"未登录"会把后者永远藏起来，而后者恰恰可能是唯一的线索
      （比如有人把 profile 目录换成了另一个浏览器的）。
    """
    path = marker_path(profile_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileMarkError(f"{path} 读不出来：{type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileMarkError(f"{path} 不是一个 JSON 对象（拿到 {type(raw).__name__}）")

    missing = [k for k in ("site", "logged_in_url", "verified_at") if not raw.get(k)]
    if missing:
        # 不猜、不给默认值：字段缺失意味着这份标记不是我们这一版写的。
        raise ProfileMarkError(f"{path} 缺字段 {missing}（版本对不上？）")
    return ProfileMark(
        site=str(raw["site"]),
        logged_in_url=str(raw["logged_in_url"]),
        verified_at=str(raw["verified_at"]),
        verified_by=str(raw.get("verified_by") or MARK_VERIFIED_BY),
    )


def write_mark(
    profile_dir: str | Path,
    *,
    site: str,
    logged_in_url: str,
    verified_at: str,
    verified_by: str = MARK_VERIFIED_BY,
) -> Path:
    """写标记。★ 调用方必须先**复核过**再调它，而不是扫码一成功就写。"""
    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    target = marker_path(path)
    target.write_text(
        json.dumps(
            {
                "site": site,
                "logged_in_url": logged_in_url,
                "verified_at": verified_at,
                "verified_by": verified_by,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def check_profile(profile_dir: str | Path | None) -> str | None:
    """用前检查。返回**要告诉用户的那句话**；一切就绪时返回 None。

    ★ 返回值是"给人看的一句话"而不是布尔：调用方（CLI）需要把它原样打出来，
      四种情况的处置各不相同，压成一个 False 就等于把"该干什么"丢掉了。
    """
    if not profile_dir:
        return (
            "任务要求登录，但没有配置持久 profile —— agent 会看到登录页，"
            "然后按任务文本停下汇报（这条路是设计好的，不是崩溃，但采不到数据）。\n"
            "    修复：人工登录一次，然后把这一行加进 .env：\n"
            f"        ECOM_AGENT_USER_DATA_DIR={DEFAULT_PROFILE_DIR}"
        )

    path = Path(profile_dir)
    if not path.is_dir():
        return (
            f"profile 目录还不存在：{path}\n"
            "    说明还没人工登录过。跑一次（会打开一个有窗口的浏览器，扫码即可）：\n"
            "        uv run python devtools/login_pdd.py"
        )

    try:
        mark = read_mark(path)
    except ProfileMarkError as exc:
        return (
            f"{exc}\n"
            "    —— 这个目录被动过或写坏了，先看看它里面到底是什么，别急着重新登录"
            "（重新登录会把现场盖掉）。"
        )

    if mark is None:
        return (
            f"{path} 里没有登录标记：可能是浏览器直接建的目录，或登录脚本没跑完。\n"
            "    重跑一次（它写标记之前会用**新会话**复核一遍）：\n"
            "        uv run python devtools/login_pdd.py"
        )
    # 有标记 ≠ 现在还有效（cookie 会过期），所以这里返回 None 只是"没有可说的"，
    # 不是"保证能跑通"。
    return None


def describe_ready(profile_dir: str | Path) -> str:
    """`check_profile` 放行时的那句回执：用**哪个**登录态跑的，要留在日志里。

    ★ 为什么值得打一行：事后要回答"这次 run 用的哪个 profile"时，
      唯一能回答的就是日志。报告和 run.json 里没有这个字段（Phase 6 的已知缺口，
      见 docs/spikes.md）—— 所以这一行不是装饰。
    """
    try:
        mark = read_mark(profile_dir)
    except ProfileMarkError:
        # 走到这儿说明 check_profile 刚放行过；真读到坏标记也不该在这里炸。
        mark = None
    if mark is None:
        return f"{profile_dir}（无登录标记）"
    return f"{profile_dir} ← {mark.describe()}"
