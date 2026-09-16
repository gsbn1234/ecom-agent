"""Phase 1 验收演示：把一份 YAML 编译成完整产物，并跑一遍决策矩阵。

    uv run python devtools/show_compiled.py
    uv run python devtools/show_compiled.py tasks/books_demo.yaml --limit 3

★ 这个脚本零浏览器、零 token、零网络 —— 它是 Phase 1 的验收物：
  "一份 YAML → 一份完整编译产物 + 一份决策矩阵测试"。
  面试时可以直接跑它，不用等浏览器启动。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecom_agent.dsl.compiler import ParamError, compile_task  # noqa: E402
from ecom_agent.dsl.loader import TaskLoadError, load_task  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="编译一份任务模板并展示产物")
    ap.add_argument("task", nargs="?", default="tasks/pdd_search_products.yaml")
    ap.add_argument("--keyword", default="保温杯")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--status", default="在售中")
    args = ap.parse_args()

    try:
        spec = load_task(args.task)
        compiled = compile_task(
            spec,
            {"keyword": args.keyword, "limit": args.limit, "status": args.status},
            chrome_path="",
        )
    except (TaskLoadError, ParamError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print("═" * 78)
    print(compiled.describe())
    print("═" * 78)

    # ── 决策矩阵 ──────────────────────────────────────────
    print("\n护栏决策演示（前缀就是 policy.evaluate 的返回）：\n")
    url = spec.start_url
    header = f"{'动作':<10} {'元素文本':<22} {'决策':<9} 命中规则"
    print(header)
    print("─" * len(header))
    for action, text in [
        ("click", "搜索"),
        ("click", "下一页"),
        ("click", "批量删除"),
        ("click", "下架"),
        ("click", "立即支付"),
        ("click", "发布商品"),
        ("click", "编辑"),
        ("click", "退出登录"),
        ("click", "一个没人写规则的新按钮"),
        ("navigate", None),
    ]:
        r = compiled.policy.evaluate(action, {}, url, text)
        # ★ 未命中规则时 rule_id 是 None —— 这恰恰是"默认 confirm 在起作用"的时刻，
        #   显示成 <default> 而不是空白，否则看不出它是被默认策略兜住的。
        print(f"{action:<10} {str(text):<22} {r.decision.value:<9} {r.rule_id or '<default>'}")

    print("\n导航前置校验：")
    for u in [
        url,
        "https://www.pinduoduo.com/",
        "https://www.taobao.com/",
        "https://item.jd.com/1.html",
        "https://example.com/",
    ]:
        r = compiled.policy.check_navigation(u)
        print(f"  {r.decision.value:<9} {u}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
