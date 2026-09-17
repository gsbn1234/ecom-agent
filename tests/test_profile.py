"""持久登录 profile 的用前检查（纯离线，零浏览器）。

★ 这个文件要守的不是"函数返回了字符串"，而是**四种情况被分得开**：

      没配 profile / 目录不存在 / 目录在但没有标记 / 标记坏了

  它们对应的处置完全不同（去配 .env、去登录、去登录、去查现场），
  压成同一个 False 就等于把"该干什么"丢掉了 —— 而 Phase 6 的整个风险
  恰恰是"人按错误的方向去排查"。所以每条断言都同时钉住
  "说了什么"和"**没有说成别的那句**"（对照）。
"""
from __future__ import annotations

import json

import pytest

from ecom_agent.config import DEFAULT_PROFILE_DIR
from ecom_agent.runtime.profile import (
    PROFILE_MARKER_NAME,
    ProfileMark,
    ProfileMarkError,
    check_profile,
    describe_ready,
    marker_path,
    read_mark,
    write_mark,
)


def test_mark_round_trips(tmp_path):
    written = write_mark(
        tmp_path,
        site="mms.pinduoduo.com",
        logged_in_url="https://mms.pinduoduo.com/goods/goods_list",
        verified_at="2026-09-17T12:00:00+00:00",
    )
    assert written.name == PROFILE_MARKER_NAME
    mark = read_mark(tmp_path)
    assert mark == ProfileMark(
        site="mms.pinduoduo.com",
        logged_in_url="https://mms.pinduoduo.com/goods/goods_list",
        verified_at="2026-09-17T12:00:00+00:00",
    )
    assert "新会话" in mark.describe() or "fresh_session" in mark.describe()


def test_mark_carries_no_secrets(tmp_path):
    """标记文件里只允许有这四个字段。

    ★ 为什么这条值得写：登录态是**最容易顺手往里加东西**的地方 ——
      今天加个 cookie，明天加个 token。而 profile 目录虽然在 .gitignore 里，
      这个文件却会被当成"我们自己的记账文件"拿出来贴给人看（我在 CLI 里就会打它）。
      钉死字段集合，等于把"能不能往这儿塞秘密"变成一个会失败的测试。
    """
    write_mark(
        tmp_path, site="s", logged_in_url="u", verified_at="t"
    )
    payload = json.loads(marker_path(tmp_path).read_text(encoding="utf-8"))
    assert set(payload) == {"site", "logged_in_url", "verified_at", "verified_by"}


def test_ready_profile_says_nothing(tmp_path):
    write_mark(tmp_path, site="s", logged_in_url="u", verified_at="t")
    assert check_profile(tmp_path) is None
    assert "s" in describe_ready(tmp_path)


def test_no_profile_configured_points_at_the_env_line():
    """没配 profile 时，这句话必须**给出可照抄的下一步**（.env 那一行）。"""
    problem = check_profile("")
    assert problem is not None
    assert "ECOM_AGENT_USER_DATA_DIR=" in problem
    # ★ 建议的那一行必须与登录脚本的默认位置**一致** ——
    #   否则用户照做之后两边指向不同目录，又回到那个静默的不一致。
    assert str(DEFAULT_PROFILE_DIR) in problem
    # 而且不能把它说成"崩溃"：这是设计内的一条收场路径。
    assert "设计好的" in problem


def test_missing_directory_points_at_the_login_script(tmp_path):
    problem = check_profile(tmp_path / "not-yet")
    assert problem is not None
    assert "devtools/login_pdd.py" in problem
    assert "不存在" in problem


def test_directory_without_mark_is_not_reported_as_ready(tmp_path):
    """★ 对照实验的核心一条：目录存在 ≠ 登录过。

    如果实现里写成 `if not path.is_dir(): warn` 然后 else 直接放行，
    这条会红 —— 而那正是"跑一次白跑"的来源：
    浏览器自己建的目录（或者上次登录脚本中途挂掉留下的目录）看起来完全正常。
    """
    (tmp_path / "Default").mkdir()  # 假装是个被 Chrome 用过的目录
    problem = check_profile(tmp_path)
    assert problem is not None, "空目录被当成了'已登录'—— 这正是那条静默失败"
    assert "没有登录标记" in problem
    assert "devtools/login_pdd.py" in problem


def test_corrupt_mark_is_told_apart_from_a_missing_one(tmp_path):
    """坏标记 ≠ 没标记：处置是"先看现场"，不是"再登一次"。

    ★ 为什么这两句必须不同：重新登录会把现场盖掉 ——
      而"标记文件坏了"本身可能就是唯一的线索（有人换了 profile 目录 / 盘写坏了）。
      把它们合成一句"未登录"，这条线索就永远看不见了。
    """
    marker_path(tmp_path).write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(ProfileMarkError):
        read_mark(tmp_path)

    problem = check_profile(tmp_path)
    assert problem is not None
    assert "别急着重新登录" in problem
    # 对照：不能说成"没有标记"（那是另一种处置）
    assert "没有登录标记" not in problem


def test_mark_with_missing_field_is_rejected_not_defaulted(tmp_path):
    """字段缺失不猜、不给默认值 —— 版本对不上就该说出来。"""
    marker_path(tmp_path).write_text(
        json.dumps({"site": "s", "logged_in_url": "u"}), encoding="utf-8"
    )
    with pytest.raises(ProfileMarkError) as exc:
        read_mark(tmp_path)
    assert "verified_at" in str(exc.value)
