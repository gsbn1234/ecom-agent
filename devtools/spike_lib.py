"""spike 专用外壳：SPIKE_XXX_OK/FAIL 输出协议 + 转出共用夹具。

★★ 这个文件现在是一层【薄壳】，夹具本体在 tests/stubs/site.py。
   为什么要挪：夹具里有几样东西是长期资产（本地站点、agent 装配、PNG 判据），
   每次 CI 都要用；而 devtools/ 是【一次性探路】的目录。
   夹具留在 devtools/ 里，测试就得反过来依赖一个本该扔掉的目录 ——
   于是"能不能删 devtools"这个问题永远没有答案，目录就烂在那里了。

   为什么不做成"拷贝一份到 tests/"：
   那样两边会各自演化。改了 site.py 的判定、忘了改 spike_lib 的，
   于是 spike 说 OK、测试说 FAIL（或反过来），而两份代码看着一模一样。
   转出（re-export）保证只有一个实现，spike 的结论和 CI 的断言永远同源。

★ 留在这里的只有 SPIKE_XXX_OK / SPIKE_XXX_FAIL 这套输出协议 ——
  它是 spike 独有的：结论要能被 grep、能被人一眼看到、能决定要不要切回退方案。
  测试不需要这套东西，测试的结论就是 pytest 的退出码。
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "tests"))

# ★ 显式列出转出的名字，而不是 `from stubs.site import *`。
#   import * 会让"spike_lib 到底提供了什么"变成一个需要去读另一个文件才能回答的问题，
#   而这个文件的存在意义恰恰是让人一眼看清 spike 能拿到什么工具。
from stubs.site import (  # noqa: F401  (re-export)
    PNG_MAGIC,
    Site,
    index_blocks,
    local_site,
    looks_like_png,
    make_action,
    make_agent,
    make_session,
)

__all__ = [
    "PNG_MAGIC",
    "Site",
    "index_blocks",
    "local_site",
    "looks_like_png",
    "make_action",
    "make_agent",
    "make_session",
    "run_spike",
    "verdict",
]


# ── 判定输出 ──────────────────────────────────────────────
def verdict(tag: str, ok: bool, detail: str = "") -> int:
    """打印 SPIKE_XXX_OK / SPIKE_XXX_FAIL: <原因>，返回进程退出码。"""
    if ok:
        print(f"\nSPIKE_{tag}_OK")
        return 0
    print(f"\nSPIKE_{tag}_FAIL: {detail}")
    return 1


def run_spike(tag: str, body: Callable[[], Any]) -> int:
    """跑一个 spike 主体。

    ★ 主体用 assert 表达成功判据：断言失败 → 报 FAIL 并带上断言消息。
      这样"成功判据"就是代码本身，而不是一段注释里的话 ——
      注释会和实现漂移，assert 不会。
    """
    try:
        asyncio.run(body())
    except AssertionError as e:
        return verdict(tag, False, str(e) or "断言失败（无消息）")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return verdict(tag, False, f"{type(e).__name__}: {e}")
    return verdict(tag, True)
