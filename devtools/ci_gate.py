"""CI 的浏览器门禁：决定"测试红了"到底算不算出事。

★★ 这个脚本存在的原因，一句话：

    原来那个 job 是 `continue-on-error: true` —— 于是**它永远不会红**。
    "浏览器测试在 CI 上挂了两周"和"浏览器测试在 CI 上一直是绿的"
    在 GitHub 页面上长得一模一样。

★ 但直接把 continue-on-error 删掉也不行，理由写在 ci.yml 的注释里：
  CI runner 上起真实 Chromium 依赖沙箱/版本/apt 包，任何一环变了都会红，
  而那是**环境问题不是代码问题**。删掉开关的结果是"CI 常常红"，而
  "老是红的 CI"和"永远绿的 CI"一样会被忽略 —— 只是更吵。

★ 所以这个脚本做的是**分辨**，不是**豁免**：

    环境不可用 + 测试红   →  warning 注解 + 绿   （这台机器跑不了，不怪代码）
    环境可用   + 测试红   →  ::error:: + 真红     （代码真的坏了，必须有人看）
    测试绿                →  绿（不管环境）

★★ 这里最容易写错、也最危险的一处，是"环境不可用"这个判据的宽窄：

    判宽一格，就多一类真失败能被洗成绿。而"红能被解释掉"本身没有价值，
    除非那个解释是**可证的**。所以判据只有一条：探针的两条启动路径**都**起不来
    （或者压根找不到 Chrome）。但凡有一条能起，就是"环境可用" —— 测试红就是真红。

    ⚠️ 反过来，"探针能起、库的路径起不来"是**特意**判成可用的：
      那正是 Phase 3 那次 CI 全红的形态（库挑中的二进制和探针挑的不是同一个），
      它看着像环境问题，实际是**我们自己的配置不一致**，该由我们看见。

★★ 关于"结论文件缺失"（探针崩了 / 没写出来 / 字段没了）：

    一律按 **usable=True**（也就是当真红）处理。这条是 fail-closed：
    一个能把红自动洗成绿的机制，比没有这个机制危险得多。
    同 guardrails 的 default_decision=confirm —— 拿不准时选更严的那边。

★★ 为什么它是脚本而不是 YAML 里的一段 shell：

    "什么时候算环境不可用"是一条**有真假**的判断逻辑，不是一次 IO。
    写成 shell 就没法单测，只能靠往 CI 推一次真的坏提交去看颜色 ——
    而那种验证方式是**不可复现**的（下一个人没法在本地重跑它）。
    放进 `tests/test_ci_gate.py` 之后，真值表（含两条对照实验）在本地毫秒级可验。
    同 `--fatal-lines` 那条纪律：**CI 只该决定【什么时候】去看，不该决定【怎么看】。**
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 退出码的语义。★ 用两个不同的非零码而不是笼统的 1：
# CI 页面上能看出"是环境问题被抬成了红"还是"测试真的红"，
# 而这两者对人的下一步动作是完全不同的。
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
"""测试真红（环境可用）。这是这个脚本唯一想让 CI 变红的情形。"""

EXIT_ERROR = 2
"""门槛脚本自己用法错了（比如没给 --tests-outcome）。

★ 刻意不复用 1：把"我用错了"和"代码坏了"混成同一个颜色，
  等于在注解区里制造一条需要解释的红 —— 而这正是本项目反复吃到的教训。
"""


def _ann(kind: str, msg: str) -> None:
    """打一行 CI 注解 + stdout 回显。转义规则与 probe_browser._ann 完全同一套。

    ★ 两处各写一份是有意的（没有互相 import）：探针是 CI 的【第一步】，
      而这个是【最后一步】。让最后一步 import 第一步的模块，就意味着
      "探针那个文件坏了"会让 gate 连启动都启动不了 —— 而 gate 起不来时，
      CI 的形态是"绿"（因为没人 exit 1）。**最后一道闸不能依赖前面的东西。**
      这段转义逻辑三行，复制它的代价远小于那个耦合的代价。
    """
    print(f"[{kind}] {msg}")
    esc = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{kind}::{esc}")


def read_usable(verdict_path: Path) -> tuple[bool, str]:
    """读探针的结论。返回 (usable, 说明)。读不到就当 usable=True。

    ★★ 这里的默认值是整个脚本最要紧的一个决定，所以它值得被单独测：
      `test_missing_verdict_file_is_treated_as_usable` 断言的就是它。
      换句话说，"探针文件缺失 → 当真红"这条**不是靠注释保证的**。
    """
    if not verdict_path.exists():
        return True, f"结论文件不存在（{verdict_path}）→ 按环境可用处理"
    try:
        data = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return True, f"结论文件读不了（{type(e).__name__}: {e}）→ 按环境可用处理"
    if not isinstance(data, dict) or "usable" not in data:
        return True, "结论文件里没有 usable 字段 → 按环境可用处理"
    # ★ 只认**严格的布尔 False**。写成 `not data["usable"]` 的话，
    #   一个 `"usable": "false"`（字符串）会被判成 True 而没有 usable 字段时
    #   反而... 总之：truthiness 在这里会让"哪个方向算坏消息"变得靠猜。
    usable = data["usable"] is not False
    reason = str(data.get("reason", "(没有 reason 字段)"))
    return usable, reason


def gate(*, tests_outcome: str, verdict_path: Path) -> tuple[int, str]:
    """纯函数：给"测试结果 + 探针结论"，返回 (退出码, 注解文案)。不打印、不读环境。

    ★ 做成纯函数是为了可测：真值表在 tests/test_ci_gate.py 里，
      而 main() 只剩下"读参数、调它、打印"这三件不需要被测的事。
    """
    ok = tests_outcome in ("success", "skipped")
    if ok:
        # ★ 测试绿的时候**不去读**结论文件。理由：绿就是绿，
        #   "环境不可用但测试绿"是一个不该出现的组合（环境不可用的话
        #   测试根本跑不起来）—— 真出现了说明我们对环境的判断错了，
        #   那时报绿仍然是安全的（没有坏事被藏起来）。
        return EXIT_OK, f"needs_browser 结果={tests_outcome} → 绿（无需看环境）"

    usable, reason = read_usable(verdict_path)
    if usable:
        return EXIT_TESTS_FAILED, (
            f"needs_browser 失败了（outcome={tests_outcome}），而探针说环境【可用】"
            f"（{reason}）→ 这是真的红，代码坏了，需要人来看。"
        )
    return EXIT_OK, (
        f"needs_browser 失败了（outcome={tests_outcome}），但探针证明环境【不可用】"
        f"（{reason}）→ 按环境问题放行（warning，不阻塞）。"
        f"⚠️ 这是**放行**不是【没红过】：测试确实跑失败了，只是不能归因到代码。"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="浏览器门禁：分辨'环境坏了'和'代码坏了'")
    p.add_argument(
        "--tests-outcome",
        required=True,
        help="上一步（跑浏览器测试）的 outcome：success / failure / skipped / cancelled",
    )
    p.add_argument(
        "--verdict-json",
        default="/tmp/browser_verdict.json",
        help="探针 --verdict-json 落下的那份结论",
    )
    args = p.parse_args(argv)

    outcome = args.tests_outcome.strip().lower()
    code, msg = gate(tests_outcome=outcome, verdict_path=Path(args.verdict_json))
    if code == EXIT_OK and outcome not in ("success", "skipped"):
        # ★ 放行也要留字。理由见 ci.yml 里那段关于"注解区总有红字"的注释的**反面**：
        #   安静地放行会让"这次真的是环境问题"变成一件没人知道的事，
        #   而它其实是一个值得知道的信号（runner 在变、依赖在变）。
        _ann("warning", msg)
    elif code == EXIT_TESTS_FAILED:
        _ann("error", msg)
    else:
        _ann("notice", msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
