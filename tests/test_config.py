"""配置中心的约定。

★ 断言的是「锚点语义」，不是硬编码路径 —— 后者换台机器就挂，
  而且这种测试失败时给不出任何有用信息（"期望 D:\\python2 得到 D:\\ci"）。

★ 本文件用自定义的 cfg 夹具而不是 monkeypatch，原因见夹具注释。
"""
import importlib
import os

import pytest

import ecom_agent.config as config


@pytest.fixture
def cfg():
    """每个用例拿到一份「干净重载」的 config，退出时把环境与模块状态都还原。

    ★ 为什么不用 monkeypatch：config.py 在【导入时】就把环境变量读成了模块级常量，
      monkeypatch 改完环境变量后必须 reload 才生效。而 reload 是全局副作用 ——
      它把模块对象本身改了，其他测试文件里已经 import 的引用会看到被污染的值。
      monkeypatch 只还原环境变量，不还原模块状态，两者在 finalize 顺序上也不保证。

      这里显式做三件事：存环境 → reload 用例内生效 → 清环境 → 再 reload 还原模块。
      多花的是一次 reload（微秒级），换来的是用例之间彻底隔离、且与执行顺序无关。
    """
    saved_env = dict(os.environ)
    importlib.reload(config)
    yield config
    os.environ.clear()
    os.environ.update(saved_env)
    importlib.reload(config)


def test_project_root_anchored_to_config_location():
    """PROJECT_ROOT 是由 config.py 自身位置推出来的，不是 CWD。

    判据：项目根下必须有 pyproject.toml。这比断言 "== D:\\python2\\ecom-agent"
    强 —— 换机器、换目录、CI 上都能用，而且它检验的正是"锚点对不对"这件事本身。
    """
    assert (config.PROJECT_ROOT / "pyproject.toml").is_file()


def test_derived_dirs_live_under_project_root():
    assert config.PROJECT_ROOT in config.RUNS_DIR.parents
    assert config.PROJECT_ROOT in config.DB_PATH.parents
    # runs/ 是运行产物目录，不需要预先存在（recorder 会 mkdir）。
    assert config.RUNS_DIR.name == "runs"


def test_chrome_path_env_override_wins(cfg):
    os.environ["ECOM_AGENT_CHROME_PATH"] = "/tmp/fake-chrome"
    assert importlib.reload(cfg).CHROME_PATH == "/tmp/fake-chrome"


def test_chrome_path_empty_string_means_autodetect(cfg):
    """★ 区分「未设置」与「设为空串」。

    未设置 → Windows 上回落到开发机默认路径（图个开箱即用）。
    设为空串 → 强制交给 browser-use 自己探测。CI 走的就是这条。
    如果实现里写成 `os.getenv(...) or DEFAULT`，空串会被 `or` 吃掉变回默认值，
    于是 CI 上会去找一台根本不存在的 Windows 机器的路径 —— 而且报错是
    "Chrome not found"，看着像环境问题，实际是配置语义错。
    """
    os.environ["ECOM_AGENT_CHROME_PATH"] = ""
    assert importlib.reload(cfg).CHROME_PATH == ""


def test_user_data_dir_is_empty_by_default(cfg):
    """★ 默认必须是"不使用持久 profile"。

    ★★ 这条守的是一个**语义**问题，不是格式问题：持久 profile 是**有状态**的
      （里面是登录 cookie）。把有状态的东西设成默认，等于让 CI 的浏览器用例
      共用一个 cookie 目录 —— 互相污染，且只在特定执行顺序下才暴露。
      默认无状态 = 每次跑都从同一个起点出发，可复现。
    """
    assert cfg.USER_DATA_DIR == ""


def test_user_data_dir_empty_string_is_not_replaced_by_the_convention(cfg):
    """★ 空串 ≠ 未设置：空串就是"我明确不要持久 profile"。

    如果实现里写成 `os.getenv(...) or DEFAULT_PROFILE_DIR`，空串会被 `or` 吃掉、
    变成"默默启用了持久 profile" —— 于是 CI 上第一次跑就写了一个
    browser_profile/ 目录，而没人下过这个决定。
    （这个坑与 CHROME_PATH 那条同源，见上面 test_chrome_path_empty_string_*。）
    """
    os.environ["ECOM_AGENT_USER_DATA_DIR"] = ""
    assert importlib.reload(cfg).USER_DATA_DIR == ""


def test_user_data_dir_env_override_wins(cfg):
    os.environ["ECOM_AGENT_USER_DATA_DIR"] = "/tmp/prof"
    assert importlib.reload(cfg).USER_DATA_DIR == "/tmp/prof"


def test_default_profile_dir_is_inside_the_project_and_gitignored():
    """★ 约定位置必须同时满足两件事：在项目里（好找）+ 被 .gitignore 挡住。

    里面是登录 cookie，提交上去 = 别人能登你的后台。所以这里直接读 .gitignore
    来验，而不是相信"我加过了"。
    """
    assert config.PROJECT_ROOT in config.DEFAULT_PROFILE_DIR.parents
    ignored = (config.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert f"{config.DEFAULT_PROFILE_DIR.name}/" in ignored


def test_live_llm_needs_both_flag_and_key(cfg):
    """双条件开关：只有 flag 没 key、或只有 key 没 flag，都必须为 False。

    任意单条件就放行的后果：CI 里有人复制了 .env 到 workflow，key 有了但没人
    开 flag，测试突然开始真的联网烧 token —— 而且是静默的。
    """
    os.environ["ECOM_AGENT_ENABLE_LIVE_LLM"] = "true"
    os.environ["DEEPSEEK_API_KEY"] = ""
    assert importlib.reload(cfg).LIVE_LLM is False

    os.environ["ECOM_AGENT_ENABLE_LIVE_LLM"] = "false"
    os.environ["DEEPSEEK_API_KEY"] = "sk-fake"
    assert importlib.reload(cfg).LIVE_LLM is False

    os.environ["ECOM_AGENT_ENABLE_LIVE_LLM"] = "true"
    os.environ["DEEPSEEK_API_KEY"] = "sk-fake"
    assert importlib.reload(cfg).LIVE_LLM is True


def test_action_timeout_raised_above_approval_timeout(cfg):
    """★ 这条守的是一个"错误信息完全指不到根因"的坑。

    browser-use 对自定义 action 有 180s 的 asyncio.wait_for 硬超时
    （tools/service.py:2196）。如果审批超时（给"人"的）比它长，
    人工还没点批准，action 就被掐了，表现为一个莫名其妙的 ActionResult(error=...)。
    config.py 主动把 action 超时抬到审批超时之上，这条断言把它钉住。

    注意：主路径（审批放 new_step_callback 里）不经过 Tools.act，不受影响；
    这是给回退方案 C 兜底的，属于"即使不用也不能踩"的那类配置。
    """
    os.environ.pop("BROWSER_USE_ACTION_TIMEOUT_S", None)
    os.environ["ECOM_AGENT_APPROVAL_TIMEOUT_S"] = "120"
    importlib.reload(cfg)
    assert int(os.environ["BROWSER_USE_ACTION_TIMEOUT_S"]) > 120


def test_setdefault_does_not_clobber_user_value(cfg):
    """用户显式设了 BROWSER_USE_ACTION_TIMEOUT_S 时，config 不能覆盖它。

    用 setdefault 而不是直接赋值，就是为了让"上游库的超时由懂行的人调"
    这条路径保持可用 —— 比如某个站点接口慢，需要把 600s 手动调上去。
    """
    os.environ["BROWSER_USE_ACTION_TIMEOUT_S"] = "999"
    os.environ["ECOM_AGENT_APPROVAL_TIMEOUT_S"] = "120"
    importlib.reload(cfg)
    assert os.environ["BROWSER_USE_ACTION_TIMEOUT_S"] == "999"
