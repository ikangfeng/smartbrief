"""可观测性事件（task_events 表）CRUD（维度一）。

一条 span 一行：图节点（node）/ LLM 调用（llm）/ 工具调用（tool）。
span 结束时由 observability.tracing.finish_span 写入完整记录
（起止时间、耗时、token、成本、错误类别），前端 Flight Recorder
按 (start_ts, parent_span_id) 渲染时间轴瀑布图，按聚合接口渲染成本卡。

设计要点：
- detail 列存 JSON（附加信息，如 llm 调用的消息数、node 的产出摘要），
  读出来自动反序列化；其余列都是标量，直接落库。
- 写入失败绝不抛出（容错：观测不能影响业务主流程）——
  tracing 层已做 try/except，这里保持纯 CRUD。
"""
import json
from typing import Any

from db.database import get_connection


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_event(row) -> dict:
    event = dict(row)
    event["detail"] = _loads(event.get("detail"), {})
    return event


def insert_event(span: dict) -> None:
    """插入一条已完成的 span 记录（span 字段见 tracing.start_span/finish_span）。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO task_events (
                task_id, span_id, parent_span_id, span_type, name, status,
                start_ts, end_ts, duration_ms,
                model, tokens_in, tokens_out, cost_usd,
                tool_name, args_hash, retry_count, error_class, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span.get("task_id"),
                span.get("span_id"),
                span.get("parent_span_id"),
                span.get("span_type"),
                span.get("name"),
                span.get("status", "ok"),
                span.get("start_ts"),
                span.get("end_ts"),
                span.get("duration_ms"),
                span.get("model"),
                span.get("tokens_in"),
                span.get("tokens_out"),
                span.get("cost_usd"),
                span.get("tool_name"),
                span.get("args_hash"),
                span.get("retry_count", 0),
                span.get("error_class"),
                json.dumps(span.get("detail") or {}, ensure_ascii=False),
            ),
        )


def list_events(task_id: str) -> list[dict]:
    """按任务拉取全部 span（按开始时间升序，前端瀑布图直接按序渲染）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY start_ts ASC, id ASC",
            (task_id,),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def summarize_events(task_id: str) -> dict:
    """按任务聚合成本/性能摘要（Flight Recorder 成本卡数据源）。

    返回：
    - llm_calls / tool_calls / node_count：三类 span 的数量
    - tokens_in / tokens_out：LLM token 合计
    - cost_usd：LLM 成本合计（美元）
    - models：按模型分组的 {calls, tokens_in, tokens_out, cost_usd}
    - tools：按工具分组的 {calls, retries, errors}
    - error_count：失败 span 数
    - total_duration_ms：任务首个 span 起点 → 末个 span 终点（墙钟耗时）
    """
    events = list_events(task_id)
    llm = [e for e in events if e["span_type"] == "llm"]
    tools = [e for e in events if e["span_type"] == "tool"]
    nodes = [e for e in events if e["span_type"] == "node"]

    models: dict[str, dict] = {}
    for e in llm:
        m = models.setdefault(
            e.get("model") or "unknown",
            {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
        )
        m["calls"] += 1
        m["tokens_in"] += e.get("tokens_in") or 0
        m["tokens_out"] += e.get("tokens_out") or 0
        m["cost_usd"] += e.get("cost_usd") or 0.0

    tool_stats: dict[str, dict] = {}
    for e in tools:
        t = tool_stats.setdefault(
            e.get("tool_name") or e.get("name") or "unknown",
            {"calls": 0, "retries": 0, "errors": 0},
        )
        t["calls"] += 1
        t["retries"] += e.get("retry_count") or 0
        t["errors"] += 1 if e.get("status") == "error" else 0

    wall_ms = 0.0
    if events:
        wall_ms = round(
            (max(e["end_ts"] for e in events) - min(e["start_ts"] for e in events)) * 1000, 1
        )

    return {
        "span_count": len(events),
        "node_count": len(nodes),
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "tokens_in": sum(e.get("tokens_in") or 0 for e in llm),
        "tokens_out": sum(e.get("tokens_out") or 0 for e in llm),
        "cost_usd": round(sum(e.get("cost_usd") or 0.0 for e in llm), 6),
        "error_count": sum(1 for e in events if e.get("status") == "error"),
        "total_duration_ms": wall_ms,
        "models": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in models.items()},
        "tools": tool_stats,
    }
