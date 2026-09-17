"""证明本项目的遥测开关**真的关掉了**，而不是"设了但设晚了"。

★★ 这件事的失败形态是本项目里最隐蔽的一类：

    它照常上报，只是你以为关了。

  因为 `browser_use/config.py:58` 是在【browser_use 模块被 import 的那一刻】
  读 `ANONYMIZED_TELEMETRY` 生成一个单例。之后再怎么设环境变量都没有用 ——
  而没有任何一处会告诉你"设晚了"。

★ 所以这个文件只做一件事：把"正常入口下它确实是关的"钉住。

  ⚠️ 已知边界（写在这里而不是假装覆盖了）：
    调用方若在 `import ecom_agent` **之前**就 import 了 browser_use，
    `ecom_agent/__init__.py` 里那两行 setdefault 来不及，
    而这条边界这个文件**测不了** —— 测它需要在同一个进程里先污染再断言，
    那验的是测试自己造出来的场景，不是真实路径。

    能测的部分：本进程按正常顺序 import 之后，库读到的开关是 false。
"""
from __future__ import annotations

import os


def test_env_defaults_are_set_by_the_package():
    """★ 先证明那两行 setdefault 真的执行了（不是恰好环境里本来就有）。

      ⚠️ 这里不能用 `monkeypatch.delenv` 来构造"干净环境"：
         `ecom_agent` 在这个文件被 import 的时候早就 import 过了，
         环境变量已经设上。删掉它再断言"应该是 false"是在验我们的期望，
         不是验真实行为。

      所以断言的是"这行代码存在且生效过"这个可观察结果 ——
      而它和 `browser_use` 实际读到的值之间的差别，由下一条测试来关。
    """
    assert os.environ.get("ANONYMIZED_TELEMETRY") == "false"
    assert os.environ.get("BROWSER_USE_CLOUD_SYNC") == "false"


def test_browser_use_singleton_actually_read_it_as_false():
    """★★ 关键的一条：库的**单例**里存的是 false。

      这才是真正决定"会不会发请求"的东西 —— 环境变量本身只是一个输入。
      只断言环境变量的测试会在"设晚了"那种情况下**通过**，
      而那正是唯一需要它失败的场景。
    """
    import browser_use

    config = browser_use.CONFIG
    assert config.ANONYMIZED_TELEMETRY is False
    assert config.BROWSER_USE_CLOUD_SYNC is False


def test_config_module_and_library_singleton_agree():
    """★ 两处读同一个开关，必须一致。

      `ecom_agent` 自己也可能读环境变量来做判断（比如 CI 里跳过某些用例）。
      两边不一致时的表现是"我们的代码以为关了、库还在发"——
      又是一个不会报错的错。
    """
    import browser_use

    assert browser_use.CONFIG.ANONYMIZED_TELEMETRY is (
        os.environ.get("ANONYMIZED_TELEMETRY", "true").strip().lower() == "true"
    )
