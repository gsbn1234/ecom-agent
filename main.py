#!/usr/bin/env python
"""项目入口 —— 真正的实现全在 `ecom_agent/runtime/cli.py`。

★ 这个文件刻意做得**很薄**，只有 import 和 sys.exit 两件事。理由：

  如果入口脚本体里写实现，那就有了**两个**"程序的开始"：
  一个在 `main.py`，一个在 `ecom_agent/`。测试只能打其中一个 ——
  于是被测的那个和用户跑的那个会慢慢分叉（最典型：参数解析在入口里写一遍、
  在包内又写一遍，然后只有一份被维护）。

  薄入口让"用户敲的命令"和"测试调的函数"是**同一个函数**。

    python main.py run tasks/books_demo.yaml
    python main.py compile tasks/books_demo.yaml --show-text
    python main.py runs
    python main.py tasks
"""
from __future__ import annotations

import sys
from pathlib import Path

# ★ 把项目根塞进 sys.path 是**兜底**，不是主路径：
#   正常情况（`uv run` / `pip install -e .`）下 ecom_agent 已经可导入。
#   但没有它的话，"直接双击 main.py"或"从别处 python D:\...\main.py"
#   会因为 cwd 不在项目根而报 ModuleNotFoundError ——
#   而那个报错完全指不到真正的原因（它会说找不到 ecom_agent，
#   让人以为是包没装好，实际只是 cwd 不对）。
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ecom_agent.runtime.cli import main  # noqa: E402 —— 必须在上面那行之后

if __name__ == "__main__":
    # ★ 把返回值交给 sys.exit 而不是吞掉：退出码是本 CLI 的对外契约
    #   （0 成功 / 1 结果不可用 / 3 被护栏拦停），CI 要靠它分支。
    raise SystemExit(main())
