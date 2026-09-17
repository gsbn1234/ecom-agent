-- ecom-agent 的库结构。两张表，刻意不加第三张。
--
-- ★ 为什么是标准库 sqlite3 而不是 SQLAlchemy：
--   这里只需要「两张表、几个查询」。SQLAlchemy 会带来 engine / session 生命周期、
--   懒加载、async 适配一整套概念 —— 而它们的收益（多方言、复杂关系、迁移）我们一条都不用。
--   标准库真正的价值是**可测性**：`Repository(tmp_path / "t.db")` 就能跑，
--   不需要 fixture、不需要建库、不需要清理。CI 里零成本。

PRAGMA foreign_keys = ON;

-- ── runs：一次 run 的头部事实 ──────────────────────────────
-- 列与 observability.models.RunRecord 一一对应。
-- ★ 刻意不建宽表：steps 不进这张表（它在 steps.jsonl 里）。
--   把每步记录塞进关系表会让"一次 run 有多少步"变成一个需要 GROUP BY 才知道的事，
--   而 JSONL 天然按行追加、崩了也不丢已写的部分。
CREATE TABLE IF NOT EXISTS runs (
    run_id                TEXT PRIMARY KEY,
    task_id               TEXT NOT NULL,
    task_name             TEXT NOT NULL DEFAULT '',
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL,

    started_at            TEXT NOT NULL,
    finished_at           TEXT NOT NULL DEFAULT '',
    duration_s            REAL NOT NULL DEFAULT 0,

    -- 可复现追溯
    task_fingerprint      TEXT NOT NULL DEFAULT '',
    browser_use_version   TEXT NOT NULL DEFAULT '',
    model                 TEXT NOT NULL DEFAULT '',
    provider              TEXT NOT NULL DEFAULT '',
    start_url             TEXT NOT NULL DEFAULT '',
    params_json           TEXT NOT NULL DEFAULT '{}',
    compiled_task_text    TEXT NOT NULL DEFAULT '',
    guardrail_policy_json TEXT NOT NULL DEFAULT '{}',

    -- 结果
    steps                 INTEGER NOT NULL DEFAULT 0,
    llm_json              TEXT NOT NULL DEFAULT '{}',
    parse_status          TEXT NOT NULL DEFAULT '',
    rows_collected        INTEGER NOT NULL DEFAULT 0,
    sanity_flags_json     TEXT NOT NULL DEFAULT '{}',
    result_raw            TEXT NOT NULL DEFAULT '',

    -- 这次 run **实测**到的登录态。★ 必须能落进 SQL 而不是只在 run.json 里：
    --   "最近这 20 次零行的 run 里，有几次其实是被登录页挡下的"是一个
    --   **该用一条 SQL 问出来**的问题。只有 JSON 文件的话，它得靠把 20 份
    --   run.json 全读一遍才答得上来 —— 那就等于没人会去问。
    --   （同样是那个家族：一个字段只在某个通道里存在，别的通道就看不见它。）
    login_state              TEXT NOT NULL DEFAULT '',
    login_state_reason       TEXT NOT NULL DEFAULT '',
    login_state_url          TEXT NOT NULL DEFAULT '',

    -- 观测自身的健康度
    screenshot_count          INTEGER NOT NULL DEFAULT 0,
    same_frame_steps_json     TEXT NOT NULL DEFAULT '[]',
    snapshot_missing_steps_json TEXT NOT NULL DEFAULT '[]',
    snapshot_overwrites       INTEGER NOT NULL DEFAULT 0,
    empty_selector_map_steps_json TEXT NOT NULL DEFAULT '[]',
    redaction_counts_json     TEXT NOT NULL DEFAULT '{}',
    unsafe_auto_approved      INTEGER NOT NULL DEFAULT 0,

    errors_json           TEXT NOT NULL DEFAULT '[]'
);

-- ★ 索引只建在这两处，理由具体：
--   (task_id, started_at DESC) —— "这个任务最近跑得怎么样"是最常被问的问题
--     （Web 层的历史列表、resume 时找上次的 run）。没有它，随着 runs 表变长，
--     这个查询会从毫秒变成全表扫。
--   status —— 找 blocked 的 run（"哪些任务被护栏拦过"）用得到。
CREATE INDEX IF NOT EXISTS idx_runs_task_started ON runs (task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);

-- ── products：一次 run 采到的行 ────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    run_id   TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,

    goods_id TEXT NOT NULL,
    title    TEXT NOT NULL,
    price    TEXT NOT NULL,
    stock    INTEGER NOT NULL,
    status   TEXT NOT NULL,

    suspicious      INTEGER NOT NULL DEFAULT 0,
    sanity_flags_json TEXT NOT NULL DEFAULT '[]',

    -- ★★ 主键是 (run_id, seq) 而【不是】(run_id, goods_id)。这是个刻意的取舍：
    --
    --   用 goods_id 做键的话，一次 run 内出现两行相同 goods_id 时会**静默覆盖**
    --   （INSERT OR REPLACE）或者报错（裸 INSERT）。前者丢掉第二行的 sanity_flags，
    --   后者让整个 run 入库失败 —— 两种都是在惩罚"数据里有个信号"。
    --
    --   而同一次采集中出现重复 goods_id 恰恰是**值得看的信号**：
    --   它意味着分页重叠（翻页后回到了上一页）或 LLM 重复输出。
    --   用 seq 做主键，两行都在 → 重复是**可见的**，
    --   再配合下面的 idx_products_goods 就能一句 SQL 查出来。
    --   这和"可疑数据只标记不删除"是同一条原则。
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_products_goods ON products (goods_id);

-- ★ price 存 TEXT 而不是 REAL。
--   SQLite 没有 DECIMAL 类型，REAL 是 IEEE754 双精度：0.1 + 0.2 != 0.3 那类问题
--   在金额上不可接受，而且它**不会报错** —— 只会在某次汇总时多出一分钱。
--   存 Decimal 的字符串形态则精确无损，比较/汇总在 Python 侧用 Decimal 做
--   （读出时 repository 会还原成 Decimal）。
--   代价是不能用 SQL 直接 SUM(price) 做数值聚合 —— 我们不需要，
--   金额汇总属于报表逻辑，应该在 Python 里显式用 Decimal 做。
