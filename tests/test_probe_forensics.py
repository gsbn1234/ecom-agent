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

from devtools.probe_browser import (
    DEATH_MARKERS,
    KNOWN_NOISE,
    fatal_lines,
    fatal_lines_for_run,
)

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

# 一条【良性】的 ERROR，逐字来自 run 35242331538 的真实 Chrome 输出。
# ★ 它与 UNKNOWN_ERROR 的区别是决定性的：UNKNOWN_ERROR 出现在"正在查失败"的
#   语境里（是线索），而这一条出现在**库启动成功**的那次运行里（是噪声）。
#   同一个字符串，该不该报，取决于"这次到底死了没有" —— 这正是本文件要锁的东西。
SSL_BENIGN = (
    "[2843:2860:0917/154630.652108:ERROR:net/socket/ssl_client_socket_impl.cc:962] "
    "handshake failed; returned -1, SSL error code 1, net_error -3"
)


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


# ── 对照实验 3b：健康运行【连嫌疑人也不许报】（2026-09-17 补）──
def test_a_healthy_run_does_not_report_suspects():
    """健康运行 + 一条【非噪声】ERROR → 一条都不许报。

    ★ 这条补的是上面那条够不着的那半。上面那条的样本只含【已知噪声】，
      而真实漏掉的是"健康运行 + 一条我们没见过的良性 ERROR"：
      兜底档把它挑成嫌疑人，注解区多出一条 error 级红字，标题写着
      「Chrome 的死因行」—— 而那次运行根本没死（库启动成功了）。

    ★ 它是**偶发**的，这才是它难被发现的原因：同一份代码前两次 CI 都没触发
      （那两次恰好没有非噪声的 ERROR 行）。偶发红字会被读成"偶尔的真事"。
    """
    text = _health_run_text() + "\n" + SSL_BENIGN

    # ★ 先证明样本是【活的】：确实含那条 ERROR，且不含任何死亡关键词。
    #   否则"报不出东西"可能只是因为样本里本来就没东西可报 —— 恒真的空转。
    assert SSL_BENIGN in text
    assert not any(k in text for k in DEATH_MARKERS)

    # 老行为不变：默认（= 正在查失败）时它是仅有线索，必须报。
    assert fatal_lines(text) == [SSL_BENIGN]

    # 闸生效：库启动成功 → 不报嫌疑人。
    assert fatal_lines_for_run(text, lib_ok=True) == []

    # 反向：库真没起来时，这条 ERROR 仍是仅有线索，不许被闸掉。
    assert fatal_lines_for_run(text, lib_ok=False) == [SSL_BENIGN]


def test_the_gate_does_not_mute_real_death_markers():
    """闸只挡嫌疑人，不挡真凶 —— 哪怕库这次起来了，真凶也要报。

    ★ 这条防的是"修过头"：把 lib_ok=True 当成"什么都可以不报"，于是真正的
      FATAL 行被一起静音。那样换来的"注解干净"是假的，代价是下一次真崩溃
      时注解区一片安静 —— 比偶发红字危险得多。
    """
    text = _crash_text()
    assert any(k in text for k in DEATH_MARKERS), "样本里没有真凶，这条测不到东西"

    out = fatal_lines_for_run(text, lib_ok=True)

    assert any(FATAL_SANDBOX in ln for ln in out), "真凶被闸掉了 —— 修过头了"
    assert any(SIGNAL_6 in ln for ln in out)


def test_include_suspects_defaults_to_the_old_behaviour():
    """默认值必须等于老行为。

    ★ 另外三个调用点（_sandbox_matrix / _forensics / 沙箱矩阵打印）一个字都没改，
      靠的就是这个默认值 —— 它们全都在"正在查失败"的语境里，报嫌疑人是对的。
    """
    for text in (_crash_text(), _health_run_text() + "\n" + SSL_BENIGN, ""):
        assert fatal_lines(text) == fatal_lines(text, include_suspects=True)


def test_the_probe_main_path_does_not_call_the_ungated_entry():
    """主路径不许绕回无闸的 `fatal_lines(log_text)`。

    ★ 这是一条**结构**断言，不是行为断言 —— 我得把它的射程说清楚：
      它证明的是"源码里没有那个写错的调用形态"，**不是**"闸真的生效了"。
      闸本身的行为由上面两条测试覆盖；而【调用点】离线测不到，
      因为探针主路径要真浏览器 + 真库（本项目里 `needs_browser` 那一档）。
      与其假装覆盖了，不如把能钉的那半钉住，并写明剩下一半没有守卫。

    ★ 为什么值得钉：这个 bug 的载体是**调用点**而不是函数 —— 函数一直是对的
      （"没有真凶时才报嫌疑人"），错的是"在健康运行里也问它要嫌疑人"。
      将来有人在失败路径上写 `fatal_lines(log_text)`，这条会拦下来，
      并把他推向 `fatal_lines_for_run(log_text, lib_ok=False)` —— 语义等价，
      但把"这次死了没有"显式写出来了，读的人不用再去猜。
    """
    from pathlib import Path

    import devtools.probe_browser as probe_browser

    src = Path(probe_browser.__file__).read_text(encoding="utf-8")
    assert "fatal_lines(log_text)" not in src, (
        "探针主路径又绕回了无闸的 fatal_lines(log_text) —— "
        "健康运行时它会把良性 ERROR 报成「Chrome 的死因行」。"
        "改用 fatal_lines_for_run(log_text, lib_ok=...)。"
    )
    # ★ 反向：确认这个断言不是在空转 —— 主路径确实调用了带闸的那个入口。
    assert "fatal_lines_for_run(log_text" in src, (
        "源码里找不到带闸的调用，这条测试是在对着空气断言"
    )


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


# ── 判决方向：两个"长得像、后果相反"的分支 ────────────────────
def test_the_two_similar_looking_failures_are_judged_oppositely():
    """★★ 对照实验：「没人指定」和「指定了但起不来」判决必须相反。

    这两件事的**表象**一样（都没有可用的 Chrome），后果却相反，
    而它们的区别只有一句话 —— **谁指定的，谁负责**：

      · 一个都没有           → 环境里确实没装 → 环境问题 → usable=False（gate 放行）
      · 有人指定了但起不来   → 是【有人写下的那行配置错了】→ 配置问题
                              → usable=True（gate 判**真红**）

    为什么要写成**成对**的断言：单看任何一条都像是随手定的默认值，
    成对摆着才能看出这是一个**非对称的设计决定**。谁哪天把它"顺手改一致"，
    这条会红，然后被迫去读那两个 docstring。
    """
    from devtools.probe_browser import no_chrome_found_verdict, spawn_failed_verdict

    # 对照组：没人指定 → 放行
    assert no_chrome_found_verdict()[0] is False
    # 实验组：指定了但起不来 → 真红
    usable, reason = spawn_failed_verdict("/nope/chrome", "FileNotFoundError: [Errno 2]")
    assert usable is True, "钉死的路径起不来被判成了『环境不可用』—— 那会把真失败洗成绿"
    # 理由里必须带上【路径】：只说"起不来"等于把死因又推回要 admin 的日志里
    assert "/nope/chrome" in reason
    assert "配置问题" in reason


def test_try_launch_reports_a_missing_binary_instead_of_raising():
    """★ 这条来自一次真 run（35205084140）：探针原来会**崩**在这里。

    崩掉的后果不是"红"（红是对的），是**红不可读** ——
    结论文件压根没写出来，gate 只能说"结论文件不存在"。

    不需要浏览器：create_subprocess_exec 会立刻抛 FileNotFoundError。
    """
    import asyncio

    from devtools.probe_browser import _try_launch

    verdict, err, code = asyncio.run(_try_launch("/definitely/not/here/chrome", [], 1))

    assert verdict == "spawn_failed", f"没抛是好事，但结论不对：{verdict!r}"
    assert code is None
    # 死因要能在注解里读出来：异常类型 + 那个路径
    assert "FileNotFoundError" in err or "No such file" in err, err
    # ★ 断言的是【路径必须出现】，而不是"异常消息里恰好有路径"：
    #   Windows 的 FileNotFoundError 消息里【没有路径】，Linux 的有。
    #   靠异常消息的写法会在 CI（Linux）上通过、在自己机器（Windows）上失效，
    #   而后者恰恰是这个错误最常撞见的地方。所以路径由我们自己拼进去。
    assert "/definitely/not/here/chrome" in err, f"注解里读不到是哪个路径起不来: {err}"
