"""探针的取证逻辑：从崩溃转储里挑出"为什么死"的那几行。

★ 为什么一个 devtools/ 里的脚本逻辑值得有测试：
  它【已经在真实环境里错过一次】，而且错法很典型 —— 按"上次看到的那条字符串"
  调白名单（写死 `dbus/bus.cc`），下一次 Chrome 从 `dbus/object_proxy.cc` 报同样的错，
  白名单没命中，注解区里那条"死因行"红字原样回来。
  这不是"手滑"，是黑名单式设计的必然结果：它保证会误报。
  所以这里测的不是"某一行能不能被过滤"，是【有真凶时会不会乱报嫌疑人】。

★ 全部是纯函数、零依赖、零 mock —— 落在项目分层里"直接单测"那一档。
  样本用的是 runner 上真实抓到的行，不是编的。
"""
from __future__ import annotations

from devtools.probe_browser import DEATH_MARKERS, KNOWN_NOISE, fatal_lines

# ── 样本：全部逐字来自 GitHub runner 上的实测输出 ──────────────
FATAL_SANDBOX = (
    "FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] "
    "No usable sandbox! If you are running on Ubuntu 23.10+ or another Linux "
    "distro that has disabled unprivileged user namespaces with AppArmor..."
)
SIGNAL_6 = "Received signal 6"
STACK_FRAME = "#12 0x55c10067d483 content::ZygoteHostImpl::Init()"

# 两类良性噪声。★ 注意 NOISE_DBUS_B 与 NOISE_DBUS_A 是【同一个子系统、
#   不同文件】—— 这正是当初那次误报的形状，必须两条都在样本里。
NOISE_DBUS_A = (
    "[2859:2859:1:ERROR:dbus/object_proxy.cc:572] Failed to call method: "
    "org.freedesktop.DBus.NameHasOwner: object_path= /org/freedesktop/DBus"
)
NOISE_DBUS_B = (
    "[2997:3022:2:ERROR:dbus/bus.cc:405] Failed to connect to the bus: "
    "Could not parse server address: Unknown address type"
)
NOISE_CPUFREQ = (
    "[0916/153651.7:ERROR:third_party/crashpad/crashpad/util/file/file_io_posix.cc:145] "
    "open /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq: No such file or directory (2)"
)

# 一条"非噪声的 ERROR"：不是死因，但也【不在】已知噪声名单里。
# 兜底档存在的意义就是它 —— 没有死亡关键词时，它是唯一线索。
UNKNOWN_ERROR = "[0916/153651.9:ERROR:some/new_subsystem.cc:42] something we have not seen before"


def _health_run_text() -> str:
    """健康运行的输出：只有良性噪声。"""
    return "\n".join([NOISE_DBUS_A, NOISE_DBUS_B, NOISE_CPUFREQ])


def _crash_text() -> str:
    """崩溃的输出：噪声 + 栈帧 + 真死因。真实顺序就是噪声在死因【前后】都有。"""
    return "\n".join(
        [NOISE_CPUFREQ, NOISE_DBUS_A, NOISE_DBUS_B, FATAL_SANDBOX, SIGNAL_6, STACK_FRAME]
    )


# ── 对照实验 1：有真凶时，只报真凶 ────────────────────────────
def test_crash_reports_death_cause_and_no_noise():
    text = _crash_text()

    # ★ 先断言输入【确实含】那些噪声 —— 否则噪声没被过滤也会"通过"，
    #   测试就变成了一个恒真的空转（doc-intel 的招牌对照手法）。
    for noise in (NOISE_DBUS_A, NOISE_DBUS_B, NOISE_CPUFREQ):
        assert noise in text, "样本本身没含这条噪声，下面的断言等于没测"

    out = fatal_lines(text)

    assert any(FATAL_SANDBOX in ln for ln in out)
    assert any(SIGNAL_6 in ln for ln in out)
    # ★ 核心断言：有真死因时，噪声一条都不许出现。
    for noise in (NOISE_DBUS_A, NOISE_DBUS_B, NOISE_CPUFREQ):
        assert not any(noise in ln for ln in out), f"有真死因时还报了噪声: {noise[:60]}"


# ── 对照实验 2：回归那次真实事故 ──────────────────────────────
def test_dbus_noise_excluded_regardless_of_source_file():
    """同一个子系统的噪声，换个文件报也必须排掉。

    ★ 这是本文件存在的直接原因。第一版白名单写死 "dbus/bus.cc"，
      下一次运行 Chrome 从 dbus/object_proxy.cc 报同样的错，红字原样回到注解里。
      断言的是【一类】而不是【一个】，这样"下次换个文件"就被覆盖了。
    """
    assert NOISE_DBUS_A in _crash_text() and NOISE_DBUS_B in _crash_text()
    out = fatal_lines(_crash_text())
    assert not any("dbus/object_proxy.cc" in ln for ln in out)
    assert not any("dbus/bus.cc" in ln for ln in out)
    # 白名单本身也该是按形态写的，不是按某次看到的字符串
    assert any(n.startswith("dbus/") for n in KNOWN_NOISE), (
        "KNOWN_NOISE 里的 dbus 项应按目录前缀匹配，不能写死某个 .cc 文件"
    )


# ── 对照实验 3：没有真凶时的兜底档 ────────────────────────────
def test_falls_back_to_unknown_errors_when_no_death_marker():
    """没有死亡关键词时，退回报非噪声的 ERROR —— 这是黑名单唯一想要的好处。

    ★ 两档制的要点：兜底【只在】primary 为空时生效。
      用一个显式含 UNKNOWN_ERROR 的样本证明它不哑巴。
    """
    text = "\n".join([NOISE_DBUS_A, NOISE_CPUFREQ, UNKNOWN_ERROR])
    assert UNKNOWN_ERROR in text and not any(k in text for k in DEATH_MARKERS)

    out = fatal_lines(text)

    assert any(UNKNOWN_ERROR in ln for ln in out), "没有真凶时连线索都不给，等于哑巴"
    assert not any(NOISE_DBUS_A in ln for ln in out), "兜底档也不该放噪声进来"


def test_healthy_run_reports_nothing():
    """健康运行 = 只有噪声 → 一条都不报。

    ★ 这是"注解通道不被自己废掉"的那条底线：
      修好之后 Chrome 起来了，注解里却还挂着红字，标题写着「死因行」。
      一份总在喊狼来了的注解，等于没有注解。
    """
    text = _health_run_text()
    assert text.strip(), "样本是空的，这个测试会假通过"
    assert fatal_lines(text) == []


# ── 去重与顺序 ────────────────────────────────────────────────
def test_dedupes_and_preserves_order():
    """同一条 FATAL 重复出现时只报一次，且保持出现顺序。

    ★ 顺序有意义：真实转储里 FATAL（原因）在 "Received signal 6"（后果）之前，
      读的人先看到原因再看到后果。用 set 实现会把这个顺序丢掉。
      样本按真实顺序写 —— 第一版这里写反了，于是"函数是对的、测试是错的"，
      而失败信息看起来像是函数打乱了顺序，很有误导性。
    """
    text = "\n".join([FATAL_SANDBOX, SIGNAL_6, FATAL_SANDBOX, SIGNAL_6])
    out = fatal_lines(text)
    assert len(out) == 2, f"没去重: {out}"
    assert FATAL_SANDBOX in out[0] and SIGNAL_6 in out[1], "顺序被打乱了"


def test_empty_input_is_empty_output():
    assert fatal_lines("") == []
    assert fatal_lines("   \n  \n") == []


# ── 注解转义：一个刚弄丢过、且丢了不会报错的纪律 ──────────────
def test_ann_escapes_for_github_annotations(capsys):
    """注解行必须按 GitHub 规则转义，且【先 % 后换行】。

    ★ 为什么值得测：这条纪律原来写在 workflow 的 shell 里（sed -e 's/%/%25/g'），
      把那段换成调用本脚本时差点丢掉。丢掉的后果是【静默的】——
      注解照样打印，只是在页面上被解析错位，而本地跑脚本完全看不出来。
      所以用 capsys 断言生成的那一行本身。

    ★ 顺序不能反：先转换行的话，%0A 里那个 % 会被第二次转义成 %250A。
    """
    from devtools.probe_browser import _ann

    _ann("error", "CPU 95% 路径含 %2F 字面量\n第二行")
    out = capsys.readouterr().out

    # stdout 那份保持可读（人直接跑脚本时看的）
    assert "[error] CPU 95% 路径含 %2F 字面量" in out
    # 注解那份转义后必须是【单行】
    ann = [ln for ln in out.splitlines() if ln.startswith("::error::")]
    assert len(ann) == 1, f"注解被换行切成了多行: {ann}"
    assert "95%25" in ann[0], "裸露的 % 没转义，GitHub 会解析错位"
    assert "%252F" in ann[0], "% 没【先】转 —— 字面量 %2F 被当成了换行标记"
    assert ann[0].rstrip().endswith("%0A第二行"), "换行没转成 %0A"
