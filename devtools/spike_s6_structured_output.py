"""S6 —— 结构化输出存盘再读回，还能不能拿到？

★ 为什么这条是"亮点 2（结构化落库）"的地基：
  我们的产物是 runs/{id}/result.json + sqlite 里的行。也就是说【一定会】发生
  "跑完 → 序列化落盘 → 之后某个时刻读回来用"这件事。如果那条路上
  history.structured_output 变成 None，那么：
    · 报告里"采集到的商品"是空的
    · 入库的行数是 0
    · 而 run 本身是成功的，没有任何报错
  一个"成功但什么都没采到"的 run，比一个失败的 run 难查得多。

★ 判据（前两条是一组对照，缺了第一条第二条就是空的）：
    1. 【对照组】刚跑完、还没落盘时，property 能拿到正确的模型
       —— 不做这一条，"落盘后是 None"就可能只是"结构化输出压根没工作"
    2. 落盘再读回，property 变成 None（而且是【静默】的，不抛异常）
    3. get_structured_output(Model) 在同一个读回对象上仍能解析出等价的模型
    4. 【兜底】就算连 getter 也不能用了，原始 JSON 字符串本身还在 final_result() 里
    5. 私有字段 _output_model_schema 根本没进序列化产物
       —— 它不是"丢了"，是"从来没被写出去"，所以也谈不上"读回来"
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use.agent.views import AgentHistoryList  # noqa: E402

from spike_lib import local_site, make_agent, run_spike  # noqa: E402
from stubs.fake_llm import FakeLLM  # noqa: E402

from ecom_agent.sites.pinduoduo.output_models import ProductRowList  # noqa: E402

TAG = "S6"

# ★ price 故意写成两种形态：
#   第一条是裸数字串，第二条带 ¥ 前缀 —— 走的是 output_models.clean_price 那条确定性清洗。
#   如果清洗没生效，ProductRowList 会因为 Decimal 校验失败而报错（这正是我们要它做的），
#   所以"能解析出来"这件事本身就证明了清洗链是通的，不需要另外断言。
_ROWS = {
    "rows": [
        {"goods_id": "100001", "title": "保温杯 316不锈钢", "price": "59.90",
         "stock": 120, "status": "在售中"},
        {"goods_id": "100002", "title": "保温杯 便携款", "price": "¥39.00",
         "stock": 45, "status": "在售中"},
    ],
    "keyword": "保温杯",
    "note": "",
}


async def body() -> None:
    with local_site() as site:
        llm = FakeLLM(script=[
            {"navigate": {"url": site.allowed + "/"}},
            # ★ 有 output_model_schema 时，done 的参数模型被换成
            #   StructuredOutputAction[ProductRowList]（tools/service.py:2010-2014），
            #   所以脚本里要写 data=...，而不是普通的 text=...。
            #   这本身就是一条事实：加了 output_model_schema 之后，
            #   LLM 的"结束"动作的 schema 变了 —— 提示词里那份 JSON schema 是它唯一的说明。
            {"done": {"data": _ROWS, "success": True}},
        ])
        agent = make_agent(
            llm,
            task="打开页面并返回结构化结果",
            agent_kw={"output_model_schema": ProductRowList},
            allowed_domains=["127.0.0.1"],
            keep_alive=True,
        )
        history = await agent.run(max_steps=4)

        # ── 判据 1：对照组 —— 落盘【之前】，property 是好的 ──
        before = history.structured_output
        print("── 判据 1（对照）：刚落盘之前 ──")
        print(f"  type={type(before).__name__}  行数={len(before.rows) if before else None}")
        assert before is not None, (
            "刚落盘时 structured_output 就是 None —— 结构化输出压根没工作，"
            "后面'落盘后变 None'的断言就成了空转（两边都是 None）"
        )
        assert isinstance(before, ProductRowList), f"拿到的不是 ProductRowList：{type(before)}"
        assert [r.goods_id for r in before.rows] == ["100001", "100002"], (
            f"行内容不对：{[r.goods_id for r in before.rows]}"
        )
        print(f"  清洗后的价格: {[str(r.price) for r in before.rows]}")
        assert before.rows[1].price == Decimal("39.00"), (
            f"'¥39.00' 没被清洗成 Decimal('39.00')，实际 {before.rows[1].price!r}"
        )
        assert before.keyword == "保温杯"

        # ── 落盘 → 读回 ───────────────────────────────────
        dumped = history.model_dump_json()
        restored = AgentHistoryList.model_validate_json(dumped)
        print(f"\n  序列化产物 {len(dumped)} 字符")

        # ── 判据 5：私有字段根本没被写出去 ────────────────
        assert "_output_model_schema" not in dumped, (
            "私有字段居然进了序列化产物 —— 那判据 2 的机制解释（'从来没写出去'）不成立"
        )
        print("── 判据 5：'_output_model_schema' 不在序列化产物里（不是丢了，是没写过）──")

        # ── 判据 2：读回之后 property 变成 None，而且是静默的 ──
        after_prop = restored.structured_output
        print("\n── 判据 2：读回之后 ──")
        print(f"  property  = {after_prop!r}")
        assert after_prop is None, (
            f"读回之后 property 竟然还能用（{after_prop!r}）—— 库修好了这件事，"
            f"docs 和 ADR-9 要改，我们也不再需要绕私有字段"
        )

        # ── 判据 3：getter 在同一个读回对象上仍然可用 ─────
        after_getter = restored.get_structured_output(ProductRowList)
        print(f"  getter    = {type(after_getter).__name__}  行数={len(after_getter.rows) if after_getter else None}")
        assert after_getter is not None, (
            "get_structured_output 也拿不到 —— 两条路都断，必须自己存原始 JSON 再 model_validate_json"
        )
        assert after_getter == before, (
            "读回来的模型和原来的不相等 —— 那么落盘这一步是有损的，比'拿不到'更麻烦"
        )

        # ── 判据 4：兜底路径 —— 原始字符串本身 ────────────
        raw = history.final_result()
        assert restored.final_result() == raw, "final_result() 没挺过序列化往返"
        print(f"\n── 判据 4：final_result() 挺过了往返，{len(raw)} 字符，可直接 model_validate_json ──")
        assert ProductRowList.model_validate_json(raw) == before, (
            "兜底路径（自己存原始 JSON）解析出来的和原来的不等价"
        )

        # ── 判据 4b：这份"原始字符串"其实【已经被清洗过了】──
        # ★ 这条是第一版断言写错之后才看清的，而它影响的是计划里的一条设计：
        #   计划写"隔离时把 final_result_raw 留着，以后能看 LLM 当时到底返回了什么"。
        #   实测：LLM 给的是 "¥39.00"，final_result() 里是 "39.00" ——
        #   因为它是 params.data.model_dump(mode='json') 的产物（tools/service.py:2017），
        #   也就是【过了 pydantic 校验和 field_validator 之后】的形态。
        #
        #   所以 final_result_raw 是"清洗后的原文"，不是"LLM 的原话"。
        #   想留 LLM 的原话，只能自己在步进记录里存 —— 而那正是 steps.jsonl 要做的事。
        #   把这两者混为一谈，排查时会对着一份"看起来正常"的数据想不通为什么会被隔离。
        assert json.loads(raw) == before.model_dump(mode="json"), (
            f"final_result() 和模型的 JSON 形态不一致：\n  raw={raw[:200]}\n"
            f"  model={json.dumps(before.model_dump(mode='json'), ensure_ascii=False)[:200]}"
        )
        assert "¥" not in raw, (
            "final_result() 里居然保留了 '¥' 前缀 —— 那它就是 LLM 的原话，"
            "本判据的结论（它是清洗后的形态）不成立"
        )
        assert '"39.00"' in raw, f"没找到清洗后的价格，raw={raw[:200]}"
        print(
            "  判据 4b：这份 raw 是【清洗后】的形态 —— LLM 给的 '¥39.00' 在这里是 '39.00'\n"
            "    它是 model_dump(mode='json') 的产物，不是 LLM 的原话。\n"
            "    → '留 raw 原文'留的是清洗后的原文；要留 LLM 原话得靠 steps.jsonl 自己记。"
        )

        # ★ 把"为什么必须用 getter"落到可执行的证据上：
        #   `_output_model_schema` 是 pydantic 的私有属性，不进 model_dump，
        #   所以读回来的对象上它是默认值 None。property 因此返回 None 而【不报错】——
        #   这正是它危险的地方：它长得像"这次没采到数据"，而不像"你调错方法了"。
        print(
            "\n【结论】读回对象上的 _output_model_schema = "
            f"{restored._output_model_schema!r}\n"
            "  它不是'丢了'，是 pydantic 私有属性从来没进过序列化产物。\n"
            "  property 于是安静地返回 None —— 看起来像'这次没采到数据'，\n"
            "  而不像'你调错方法了'。落库/报告一律走 get_structured_output(Model)。"
        )

        await agent.browser_session.kill()


if __name__ == "__main__":
    raise SystemExit(run_spike(TAG, body))
