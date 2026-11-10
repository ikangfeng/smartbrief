"""SQLite 连接管理与建表（第 24/25/26/27 节：任务业务数据持久化）。

职责边界：本模块负责**业务数据**（任务 / ReAct 日志 / 评测报告）的持久化；
第 26 节起，LangGraph 的**图运行时状态**（Checkpointer）由独立库文件
`checkpoints.db` 承载（见 memory/checkpointer.py）—— 两者职责分离、互不干扰：
- data.db        存"任务最终长什么样"（tasks / task_logs / evaluation_reports，业务层可查）
- checkpoints.db 存"图执行到哪一步了"（节点级快照，仅框架层可读）

第 27 节新增 `evaluation_reports` 表：自动化评测的执行进度与最终报告
（JSON 指标 + Markdown 可视化总结），供前端「评测中心」查询历史。
"""
import sqlite3
from pathlib import Path

# 业务 SQLite 库文件（backend/data.db）
DB_PATH = Path(__file__).resolve().parents[1] / "data.db"

# 建表 SQL（幂等：IF NOT EXISTS 保证重复执行不报错）
_SCHEMA = """
-- 任务主表：一个 task_id 对应一条任务记录
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,   -- 任务唯一 id（API 层生成，形如 task_ab12cd34）
    topic               TEXT NOT NULL,      -- 用户提交的研究主题
    status              TEXT NOT NULL DEFAULT 'running',  -- running | paused（服务中断）| completed | failed
    plan                TEXT,          -- JSON 数组（步骤列表 [{"id":1,"agent":"researcher",...}]）
    current_step_index  INTEGER DEFAULT 0,  -- 当前进度（= 已完成步骤数）
    completed_steps     TEXT,          -- JSON 数组（已完成步骤 id，如 "[1,2]"）
    research_data       TEXT,          -- JSON 对象（各步骤研究结果）
    draft_content       TEXT,          -- 报告草稿（Markdown）
    final_report        TEXT,          -- 最终报告（第 24 节先复用草稿）
    error               TEXT,          -- 任务失败原因（status=failed 时填充）
    review_comments     TEXT,          -- JSON 数组（Reviewer 最后一次审核意见，第 25 节）
    review_history      TEXT,          -- JSON 数组（A2A 各轮审核记录 [{"round","passed","comments"}]，按轮保留）
    iteration           INTEGER DEFAULT 0,   -- A2A 循环次数（每次打回 +1）
    max_iterations      INTEGER DEFAULT 3,   -- A2A 循环上限（达上限强制通过；仅供前端展示，值由 workflow.run_task 落库）
    passed              INTEGER,       -- 最近一次审核是否通过（0/1/NULL 未审核；强制通过时亦为 1，原始判定见 review_history）
    forced_pass         INTEGER DEFAULT 0,   -- 是否因达循环上限被强制通过（0/1）
    memory_hits         TEXT,          -- JSON 数组（第 26 节：本次任务的长期记忆命中 [{"topic","score",...}]）
    created_at          TEXT DEFAULT (datetime('now','localtime')),  -- 创建时间
    updated_at          TEXT DEFAULT (datetime('now','localtime'))   -- 最后更新时间
);

-- 审批记录表（维度二）：人工审批闸门的留痕（计划闸门 plan / 报告闸门 report）。
-- 一次闸门触发产生一行：提交审批时快照待审内容（payload_snapshot + payload_hash），
-- 人工决策后更新 status（pending → approved / edited / rejected）。
-- version 为乐观锁：前端决策需带上读取时的 version，并发冲突返回 409。
CREATE TABLE IF NOT EXISTS task_approvals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL,             -- 归属任务
    gate             TEXT NOT NULL,             -- plan（计划闸门）| report（报告闸门）
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | edited | rejected
    payload_snapshot TEXT NOT NULL,             -- 提交审批时的待审内容快照（JSON）
    payload_hash     TEXT NOT NULL,             -- 快照 sha256 前 12 位（防篡改比对）
    version          INTEGER NOT NULL DEFAULT 1,-- 乐观锁版本号（每次决策 +1）
    approver         TEXT,                      -- 审批人（演示环境手工填写）
    comment          TEXT,                      -- 审批意见
    edited_payload   TEXT,                      -- 「改后批准」时的人工修订内容（JSON）
    decided_at       TEXT,                      -- 决策时间
    created_at       TEXT DEFAULT (datetime('now','localtime')),
    updated_at       TEXT DEFAULT (datetime('now','localtime'))
);

-- 查询加速：按任务查审批记录
CREATE INDEX IF NOT EXISTS idx_task_approvals_task ON task_approvals(task_id);

-- ReAct 日志表：一条任务对应多条日志（Thought/Action/Observation）
CREATE TABLE IF NOT EXISTS task_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键（日志按此排序）
    task_id     TEXT NOT NULL REFERENCES tasks(task_id),  -- 归属任务（外键）
    step_id     INTEGER NOT NULL,   -- 归属 plan 步骤 id（前端按步骤分组展示）
    log_type    TEXT NOT NULL,      -- Thought | Action | Observation
    content     TEXT NOT NULL,      -- 日志正文
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 查询加速：按 task_id 查日志走索引
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id);

-- 评测报告表（第 27 节）：自动化评测的执行记录与最终报告
CREATE TABLE IF NOT EXISTS evaluation_reports (
    eval_id         TEXT PRIMARY KEY,   -- 评测唯一 id（形如 eval_ab12cd34）
    status          TEXT NOT NULL DEFAULT 'running',  -- running（执行中）| completed | failed
    total_cases     INTEGER NOT NULL,   -- 本次数据集用例总数（standard=10 / smoke=2）
    completed_cases INTEGER DEFAULT 0,  -- 已完成用例数（前端进度条数据源，执行中轮询）
    results         TEXT,               -- JSON 数组（每个用例的执行结果：耗时/ReAct次数/关键词覆盖率…）
    metrics         TEXT,               -- JSON 对象（8 项指标实测值与目标值达标情况）
    report_md       TEXT,               -- Markdown 可视化总结（含指标表 + 用例明细表 + 回归结论）
    -- 维度三：数据集化评测 + LLM-as-Judge + 回归门禁
    dataset         TEXT DEFAULT 'standard',  -- 本次评测使用的数据集（standard / smoke）
    judge_model     TEXT,               -- LLM-as-Judge 使用的模型名（未启用为 NULL）
    regression      TEXT,               -- JSON 对象（与基线对比的回归结论：gate / blockers / deltas）
    config          TEXT,               -- JSON 对象（本次评测配置：dataset/use_judge/baseline_eval_id/approval_mode）
    error           TEXT,               -- 评测失败原因（status=failed 时填充）
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- 可观测性事件表（维度一）：一次任务的全部 span（图节点 / 工具调用 / LLM 调用）
-- 一条 span 一行（span 结束时写入完整记录：起止时间、耗时、token、成本、错误类别）。
-- parent_span_id 构成树：node span 为父，其内部的 llm/tool span 为子，
-- 前端 Flight Recorder 据此渲染时间轴瀑布图与成本卡。
CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,              -- 归属任务
    span_id         TEXT NOT NULL,              -- span 唯一 id（uuid8）
    parent_span_id  TEXT,                       -- 父 span id（顶层 node span 为 NULL）
    span_type       TEXT NOT NULL,              -- node | llm | tool
    name            TEXT NOT NULL,              -- 展示名：节点名 / 模型名 / 工具名
    status          TEXT NOT NULL DEFAULT 'ok', -- ok | error
    start_ts        REAL NOT NULL,              -- span 开始（epoch 秒）
    end_ts          REAL NOT NULL,              -- span 结束（epoch 秒）
    duration_ms     REAL NOT NULL,              -- 耗时（毫秒）
    model           TEXT,                       -- LLM span：模型名
    tokens_in       INTEGER,                    -- LLM span：prompt tokens
    tokens_out      INTEGER,                    -- LLM span：completion tokens
    cost_usd        REAL,                       -- LLM span：本次调用估算成本（美元）
    tool_name       TEXT,                       -- tool span：工具名
    args_hash       TEXT,                       -- tool span：参数 sha256 前 12 位（可区分调用又不落明文）
    retry_count     INTEGER DEFAULT 0,          -- tool span：重试次数
    error_class     TEXT,                       -- 错误类别（异常类名，如 TimeoutError）
    detail          TEXT,                       -- JSON 附加信息（如 llm 调用轮次、node 摘要）
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- 查询加速：按任务拉取全部 span 走索引
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_parent ON task_events(task_id, parent_span_id);
"""


# 第 25 节新增列的迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的旧表加列，
# 这里用 ALTER TABLE ADD COLUMN 逐列补齐（列已存在时抛 OperationalError → 跳过，幂等）。
# 注意：加列必须同步修改 _SCHEMA、task_repository 的 _JSON_FIELDS/_ALLOWED_UPDATE、
# workflow.run_task 落库与前端类型，五处缺一不可（见 CLAUDE.md 约定 12）。
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN review_comments TEXT",
    "ALTER TABLE tasks ADD COLUMN review_history TEXT",
    "ALTER TABLE tasks ADD COLUMN iteration INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN max_iterations INTEGER DEFAULT 3",
    "ALTER TABLE tasks ADD COLUMN passed INTEGER",
    "ALTER TABLE tasks ADD COLUMN forced_pass INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN memory_hits TEXT",
    # 维度一（可观测性）：断点信息卡 —— 恢复次数与最近恢复时间
    "ALTER TABLE tasks ADD COLUMN resume_count INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN last_resume_at TEXT",
    # 维度二（功能控制）：审批模式 + 执行预算护栏
    # approval_mode：manual（双闸门人工审批）| auto（自动放行，Harness 跑批用）
    "ALTER TABLE tasks ADD COLUMN approval_mode TEXT DEFAULT 'manual'",
    "ALTER TABLE tasks ADD COLUMN max_tokens INTEGER",       # token 预算上限（NULL 不限）
    "ALTER TABLE tasks ADD COLUMN max_duration_s INTEGER",   # 时长预算上限秒（NULL 不限）
    "ALTER TABLE tasks ADD COLUMN budget_exceeded INTEGER DEFAULT 0",  # 预算超限强制收口标志
    # 维度三（Harness 评测升级）：数据集 / Judge 模型 / 回归结论 / 评测配置
    "ALTER TABLE evaluation_reports ADD COLUMN dataset TEXT DEFAULT 'standard'",
    "ALTER TABLE evaluation_reports ADD COLUMN judge_model TEXT",
    "ALTER TABLE evaluation_reports ADD COLUMN regression TEXT",
    "ALTER TABLE evaluation_reports ADD COLUMN config TEXT",
]


def get_connection() -> sqlite3.Connection:
    """新建一条 SQLite 连接（行以 dict 风格访问，允许跨线程使用）。

    教学要点：每次操作**新建连接、用完即关**。
    FastAPI 是异步多线程环境，如果长连接跨线程共享，会报
    "SQLite objects created in a thread can only be used in that thread"。
    频繁开关连接的性能开销对本项目量级可忽略，换来的是线程安全。
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # 行记录支持 dict 风格访问（row["task_id"]）
    return conn


def init_db(db_path: Path | None = None) -> None:
    """初始化数据库：可选覆盖库文件路径（测试用临时库），并执行建表 SQL（幂等）。

    :param db_path: 指定库文件路径；不传则用默认 backend/data.db
                    （测试/notebook 演示可通过它隔离出临时库，避免污染真实数据）
    """
    global DB_PATH
    if db_path is not None:
        DB_PATH = Path(db_path)             # 覆盖全局库路径（模块级单例）
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_SCHEMA)         # 批量执行建表 SQL（IF NOT EXISTS → 幂等）
        for sql in _MIGRATIONS:             # 旧库补列（已存在则跳过）
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
