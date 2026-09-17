"""SQLite 落库。

★ 为什么同步（不套 `asyncio.to_thread`）：
  这里的写发生在**一次 run 结束时**，不是每步。一次写入是"1 行 runs + N 行 products"，
  N 是几十的量级，耗时在毫秒级 —— 和 RunRecorder 每步写几百 KB 截图完全不是一回事。
  为它引入 to_thread 只会把"事务边界"这件事从一处 SQL 摊到两条执行路径上。

★ 事务边界是整个落库设计里唯一重要的东西：
  **runs 行与它的全部 products 行必须在同一个事务里提交。**
  否则崩溃会留下"runs 说有 20 行、products 里只有 7 行"的记录 ——
  而这份记录看起来完全正常，直到有人拿它去对账。
  反过来说，"runs 行存在而 products 零行"是**合法的已提交状态**
  （quarantine：schema 不合法，整体拒绝入库，raw 原文留着）——
  所以不能靠"products 有行"来判断 run 成功，得看 `parse_status`。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Sequence

from ecom_agent.observability.models import RunRecord
from ecom_agent.observability.redact import Redactor
from ecom_agent.sites.pinduoduo.output_models import ProductRow

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# ★ schema.sql 之后【追加】的列。两者必须同时改：schema.sql 让新库一开始就有，
#   这里让老库补上。只改一处的话，新库和老库会分叉 —— 而分叉表现为
#   "在我机器上是好的"（我的库是新键的）。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("empty_selector_map_steps_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("login_state", "TEXT NOT NULL DEFAULT ''"),
    ("login_state_reason", "TEXT NOT NULL DEFAULT ''"),
    ("login_state_url", "TEXT NOT NULL DEFAULT ''"),
)
"""★ empty_selector_map_steps_json —— S2-4 探测器的落库列。见 RunRecord 的同名字段。

★ 三条 login_state_* —— "这次到底是登进去没有"的落库列。见 RunRecord.login_state。
  ⚠️ 老库（Phase 6 之前建的、以及真站点首跑那个 `runs/ecom_agent.db`）**没有**这三列，
  而那个库是审计资产 —— 只能加列，不能重建。这就是 `_migrate()` 存在的理由：
  默认值 `''` 对老行是**诚实**的（那时确实没探过），不是"填个假值糊过去"。
"""


class Repository:
    """一个 SQLite 库的读写门面。

    ★ 连接在构造时建立并持有：SQLite 的连接是有状态的（PRAGMA、事务），
      每调一次开一个连接会让"我设的 PRAGMA 还生效吗"变成一个需要追问的问题。
      用法上它就是个 `with` 块 —— 见 `__enter__`。

    ★ 也可不写 `with`：`Repository(path)` 自己会建表。
      这么做是为了让测试最省事 —— `Repository(tmp_path / "t.db")` 一行可用，
      而不需要记得先调一次 init。
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent and not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        """★ `isolation_level=None` = 关掉 Python 的隐式事务管理，改由我们显式 BEGIN。

        理由：sqlite3 模块默认会在 INSERT 前自动开事务、在非 DML 语句前自动提交 ——
        这套隐式规则意味着"什么时候提交"取决于我们调了什么语句，
        而我们要的恰恰是"runs 和 products 要么一起进去要么都不进去"。
        显式 BEGIN/COMMIT 让这件事由代码说了算。
        """
        self.conn.row_factory = sqlite3.Row
        # ★ 外键约束 SQLite 默认是【关】的，每连接都要开一次。
        #   不开的话 products 表上的 REFERENCES 只是一句注释：删掉 runs 行不会级联，
        #   products 里会留下孤儿行，而查询照常返回它们。
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL：读不阻塞写。Web 层一边读历史一边有 run 在写，这是默认要支持的。
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()

    def _migrate(self) -> None:
        """把已存在的老库补齐到当前 schema。

        ★★ 为什么需要这个函数，而不是"删了重建"：
          schema.sql 里是 `CREATE TABLE IF NOT EXISTS` —— 对**已存在**的表它是空操作。
          所以给 runs 加一列时，老库不会自动获得那一列，随后 `save_run` 会以
          `no such column` 失败（一次 run 跑完、产物全写完，最后一步落库时炸）。

          而这个库是**审计资产**。处置方式只有一种是对的：加列。
          删库重建等于把全部历史 run 丢掉 —— 而"历史 run"恰恰是护栏效果、
          LLM 成本趋势这些问题的唯一数据来源。

        ★ 只做 ADD COLUMN：SQLite 的 ALTER 基本也只支持这个，而它正好覆盖
          "向上加字段"这个唯一的演进方向（改类型/删列在这个 schema 里没有正当理由）。
        ★ 新加的列必须自带默认值且非空 —— SQLite 对已有的行要用它去填。
        """
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(runs)")}
        for name, ddl in _ADDED_COLUMNS:
            if name in existing:
                continue
            logger.info("给已存在的 runs 表补列：%s", name)
            self.conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")

    # ── 生命周期 ──────────────────────────────────────────
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── 写 ────────────────────────────────────────────────
    def save_run(
        self,
        record: RunRecord,
        rows: Iterable[ProductRow] = (),
        *,
        redactor: Redactor | None = None,
    ) -> int:
        """落一次 run（头部 + 采到的行），单事务。返回写入的行数。

        ★ 在**序列化之前**脱敏（和 steps.jsonl / run.json 同一条纪律）。
          范围：run 记录整体（含 `result_raw`、`compiled_task_text`、错误文本）。
          不予脱敏：`products` 的行数据 —— 理由见 `_product_payload`。
        """
        redactor = redactor or Redactor()
        payload = redactor.obj(record.model_dump(mode="json"))
        product_payloads = [
            self._product_payload(r, run_id=payload["run_id"], seq=i)
            for i, r in enumerate(rows)
        ]

        # ★ 标题里的联系方式只标记、不替换 —— 见 `_product_payload` 的说明。
        #   合并进 run 的 sanity_flags，这样报告和查询都能看到"这批数据里有几行带联系方式"。
        #   ★ pop 掉 pii_flags：它只是这里用来合并的临时字段，不是 products 表的列。
        #     留着的话它会作为多余键混进 executemany 的参数字典 ——
        #     当前版本会忽略多余键，但"依赖驱动忽略我多给的字段"是个不该有的依赖。
        for p in product_payloads:
            pii = p.pop("pii_flags")
            if pii:
                payload["sanity_flags"][p["goods_id"] or f"seq-{p['seq']}"] = pii

        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_id, task_name, attempt, status,
                    started_at, finished_at, duration_s,
                    task_fingerprint, browser_use_version, model, provider,
                    start_url, params_json, compiled_task_text, guardrail_policy_json,
                    steps, llm_json, parse_status, rows_collected,
                    sanity_flags_json, result_raw,
                    login_state, login_state_reason, login_state_url,
                    screenshot_count, same_frame_steps_json, snapshot_missing_steps_json,
                    snapshot_overwrites, empty_selector_map_steps_json,
                    redaction_counts_json, unsafe_auto_approved,
                    errors_json
                ) VALUES (
                    :run_id, :task_id, :task_name, :attempt, :status,
                    :started_at, :finished_at, :duration_s,
                    :task_fingerprint, :browser_use_version, :model, :provider,
                    :start_url, :params_json, :compiled_task_text, :guardrail_policy_json,
                    :steps, :llm_json, :parse_status, :rows_collected,
                    :sanity_flags_json, :result_raw,
                    :login_state, :login_state_reason, :login_state_url,
                    :screenshot_count, :same_frame_steps_json, :snapshot_missing_steps_json,
                    :snapshot_overwrites, :empty_selector_map_steps_json,
                    :redaction_counts_json, :unsafe_auto_approved,
                    :errors_json
                )
                """,
                {
                    "run_id": payload["run_id"],
                    "task_id": payload["task_id"],
                    "task_name": payload["task_name"],
                    "attempt": payload["attempt"],
                    # ★ 状态用 RunRecord 自己的值，不用调用方传的"成功与否"。
                    #   一次 run 有四种结局（completed / failed / blocked / running），
                    #   让 store 去猜"这次算不算成功"必然猜错 BLOCKED 和 FAILED 的区别。
                    "status": payload["status"],
                    "started_at": payload["started_at"],
                    "finished_at": payload["finished_at"],
                    "duration_s": payload["duration_s"],
                    "task_fingerprint": payload["task_fingerprint"],
                    "browser_use_version": payload["browser_use_version"],
                    "model": payload["model"],
                    "provider": payload["provider"],
                    "start_url": payload["start_url"],
                    "params_json": _j(payload["params"]),
                    "compiled_task_text": payload["compiled_task_text"],
                    "guardrail_policy_json": _j(payload["guardrail_policy"]),
                    "steps": payload["steps"],
                    "llm_json": _j(payload["llm"]),
                    "parse_status": payload["parse_status"],
                    "rows_collected": payload["rows_collected"],
                    "sanity_flags_json": _j(payload["sanity_flags"]),
                    "result_raw": payload["result_raw"],
                    # ★ 登录态三列。★ 注意它取自 RunRecord 而不是调用方 ——
                    #   "有没有登录"是 run 自己观察到的事实，不该由谁来转述。
                    "login_state": payload["login_state"],
                    "login_state_reason": payload["login_state_reason"],
                    "login_state_url": payload["login_state_url"],
                    "screenshot_count": payload["screenshot_count"],
                    "same_frame_steps_json": _j(payload["same_frame_steps"]),
                    "snapshot_missing_steps_json": _j(payload["snapshot_missing_steps"]),
                    "snapshot_overwrites": payload["snapshot_overwrites"],
                    # ★ 这一列是【事后补的】。补之前它只在 RunRecord 和 run.json 里有，
                    #   而"哪些步的 selector_map 是空的"恰恰是最该能用 SQL 查的东西
                    #   （"最近 20 次 run 里有几次护栏其实一步没判"）——
                    #   只有 JSON 文件的话，这个问题得靠把 20 份 run.json 全读一遍才答得上来。
                    "empty_selector_map_steps_json": _j(payload["empty_selector_map_steps"]),
                    "redaction_counts_json": _j(payload["redaction_counts"]),
                    "unsafe_auto_approved": int(bool(payload["unsafe_auto_approved"])),
                    "errors_json": _j(payload["errors"]),
                },
            )
            if product_payloads:
                self.conn.executemany(
                    """
                    INSERT INTO products
                        (run_id, seq, goods_id, title, price, stock, status,
                         suspicious, sanity_flags_json)
                    VALUES (:run_id, :seq, :goods_id, :title, :price, :stock, :status,
                            :suspicious, :sanity_flags_json)
                    """,
                    product_payloads,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return len(product_payloads)

    # ── 读 ────────────────────────────────────────────────
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_products(self, run_id: str) -> list[dict[str, Any]]:
        """取一次 run 采到的行。price 还原成 Decimal。

        ★ 还原成 Decimal 而不是留着 str：调用方拿到 str 会忍不住去做字符串比较
          （`"9.00" > "10.00"` 为真），而那是**静默的**排序错误。
          从库里读出来的东西应该已经是可以运算的类型。
        """
        rows = self.conn.execute(
            "SELECT * FROM products WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["price"] = Decimal(d["price"])
            d["sanity_flags"] = json.loads(d.pop("sanity_flags_json"))
            d["suspicious"] = bool(d["suspicious"])
            out.append(d)
        return out

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """最近的 run 列表（新的在前）。★ 走 idx_runs_task_started 之外的路径 ——
        纯按时间排序，所以这里不需要额外索引：runs 表本身不会大到需要它。"""
        rows = self.conn.execute(
            "SELECT run_id, task_id, task_name, status, started_at, duration_s, "
            "parse_status, rows_collected, login_state FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def runs_for_task(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """某个任务的历史（新的在前）。★ 这条走 idx_runs_task_started。"""
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def duplicate_goods_ids(self, run_id: str) -> list[str]:
        """一次 run 内出现多次的 goods_id。

        ★ 这个方法就是 (run_id, seq) 主键那个取舍的回报：
          如果主键是 (run_id, goods_id)，重复行根本没机会存在，这个查询也就无从写起 ——
          "分页重叠 / LLM 重复输出"这个信号会在入库那一刻被静默处理掉。
        """
        rows = self.conn.execute(
            "SELECT goods_id, COUNT(*) AS n FROM products WHERE run_id = ? "
            "GROUP BY goods_id HAVING n > 1 ORDER BY n DESC",
            (run_id,),
        ).fetchall()
        return [r["goods_id"] for r in rows]

    # ── 行 → 落库用的 dict ─────────────────────────────────
    @staticmethod
    def _product_payload(row: ProductRow, *, run_id: str, seq: int) -> dict[str, Any]:
        """把一行商品转成可插入的 dict。

        ★★ 这里有一条刻意的【不脱敏】决定，必须说清楚，因为它看起来像漏了：

          商品标题**不做替换式脱敏**，只做**检测 + 标记**（pii_flags）。

          替换掉的话，库里存的就不是页面上真实的值了 —— 而"标题里有没有联系方式"
          本身是有意义的业务信息（部分卖家会往标题里塞电话）。
          标题被洗成 `<REDACTED:phone>` 之后，报告看起来"脱敏很到位"，
          但采集任务的核心产出被静默改了，而且没人能看出改过。

          所以：**行数据保真，可疑点在报告里显式标出，由人决定怎么处置。**
          这和"可疑数据只标记不删除"是同一条原则的两个应用。

          注意这条规则只适用于**卖家自己的商品数据**。买家 PII 是另一回事：
          那个是绝对红线，任何情况下都不导出 —— 见 docs/guardrail_design.md。
        """
        flags = list(row.sanity_flags())
        pii = _detect_pii(row.title)
        flags.extend(f"pii_in_title:{k}" for k in pii)

        return {
            "run_id": run_id,
            "seq": seq,
            "goods_id": row.goods_id,
            "title": row.title,
            "price": str(row.price),  # ★ Decimal → str，不是 float：见 schema.sql 的说明
            "stock": row.stock,
            "status": row.status,
            "suspicious": int(bool(flags)),
            "sanity_flags_json": _j(flags),
            "pii_flags": flags if pii else [],
        }


# ── 模块级小工具 ──────────────────────────────────────────
def _j(value: Any) -> str:
    """紧凑 JSON。★ ensure_ascii=False：中文标题存成 \\uXXXX 的话，
    直接用 sqlite3 命令行看库时全是转义序列，等于不能看。"""
    return json.dumps(value, ensure_ascii=False)


def _detect_pii(text: str) -> list[str]:
    """检测一段文本里有没有敏感形态，返回命中的种类。

    ★ 复用 `Redactor` 的规则表，而不是另写一套正则：
      两套规则必然会漂移 —— 那时会出现"脱敏盖住了但检测没发现"（漏报）
      或者反过来（误报），而两种都只能靠人去比对两个文件才能发现。
      同一份规则表让这件事在结构上不可能发生。

    ★ 用**新建的** Redactor 而不是共享一个实例：Redactor 的 counts 是累积的，
      共享实例在 `to_thread` 场景下还涉及跨线程读写。新建一个便宜（正则已缓存），
      而且天然没有状态共享问题。
    """
    probe = Redactor()
    probe.text(text or "")
    return sorted(probe.counts)
