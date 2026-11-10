"""审批记录 CRUD（task_approvals 表，维度二：人工审批闸门留痕）。

一次闸门（gate_plan / gate_report）触发产生一行 pending 记录：
- payload_snapshot / payload_hash：提交审批时的待审内容快照与其 sha256 指纹，
  防止"审批看到的内容"与"实际放行的内容"不一致（可比对哈希）。
- version：乐观锁。前端 POST 决策必须带上读取时的 version，
  UPDATE ... WHERE id=? AND version=? AND status='pending' 影响 0 行即并发冲突（409）。

决策动作三选一：approve（批准）/ edited（改后批准，存 edited_payload）/ rejected（退回）。
"""
import hashlib
import json
from typing import Any

from db.database import get_connection

# 合法决策动作（与前端三按钮一致）
ACTIONS = ("approve", "edit", "reject")
# 决策后的终态映射（edit → edited）
_ACTION_TO_STATUS = {"approve": "approved", "edit": "edited", "reject": "rejected"}


def payload_hash(payload: Any) -> str:
    """待审内容指纹：JSON 规范化序列化后的 sha256 前 12 位。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _row_to_approval(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["payload_snapshot"] = json.loads(item["payload_snapshot"])
    if item.get("edited_payload"):
        item["edited_payload"] = json.loads(item["edited_payload"])
    return item


def create_pending(task_id: str, gate: str, payload: Any) -> dict:
    """创建一条待审批记录（闸门节点 interrupt 前调用，幂等性由调用方保证）。

    :param task_id: 任务 id
    :param gate: 闸门标识（plan / report）
    :param payload: 待审内容（计划列表或草稿文本）
    :return: 新建记录（含 id / version=1）
    """
    snapshot = json.dumps(payload, ensure_ascii=False)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO task_approvals (task_id, gate, payload_snapshot, payload_hash) "
            "VALUES (?, ?, ?, ?)",
            (task_id, gate, snapshot, payload_hash(payload)),
        )
        row = conn.execute(
            "SELECT * FROM task_approvals WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_approval(row)


def get_current(task_id: str, gate: str | None = None) -> dict | None:
    """取某任务最近一次审批记录（可按闸门过滤）；无记录返回 None。"""
    sql = "SELECT * FROM task_approvals WHERE task_id = ?"
    params: list = [task_id]
    if gate:
        sql += " AND gate = ?"
        params.append(gate)
    sql += " ORDER BY id DESC LIMIT 1"
    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return _row_to_approval(row)


def get_pending(task_id: str) -> dict | None:
    """取某任务当前待审批记录（status=pending 的最新一条）；无则返回 None。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM task_approvals WHERE task_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return _row_to_approval(row)


def list_approvals(task_id: str) -> list[dict]:
    """某任务的全部审批记录（按 id 升序，前端审批历史用）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM task_approvals WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()
    return [_row_to_approval(r) for r in rows]


def decide(
    approval_id: int,
    action: str,
    version: int,
    approver: str | None = None,
    comment: str | None = None,
    edited_payload: Any = None,
) -> dict | None:
    """对一条待审批记录做决策（乐观锁）。

    仅当记录仍是 pending 且 version 匹配时才更新成功：
    - 已被人抢先决策（status 不再是 pending）→ 返回 None（调用方返回 409）
    - version 不匹配（页面打开后记录被更新）→ 返回 None（调用方返回 409）

    :param action: approve / edit / reject
    :param version: 前端读取记录时的 version
    :return: 决策后的记录；冲突返回 None
    """
    if action not in ACTIONS:
        raise ValueError(f"非法审批动作：{action}")
    status = _ACTION_TO_STATUS[action]
    edited_json = (
        json.dumps(edited_payload, ensure_ascii=False) if edited_payload is not None else None
    )
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE task_approvals SET status = ?, approver = ?, comment = ?, "
            "edited_payload = ?, version = version + 1, "
            "decided_at = datetime('now','localtime'), "
            "updated_at = datetime('now','localtime') "
            "WHERE id = ? AND version = ? AND status = 'pending'",
            (status, approver, comment, edited_json, approval_id, version),
        )
        if cur.rowcount == 0:
            return None                                    # 乐观锁冲突 / 已被决策
        row = conn.execute(
            "SELECT * FROM task_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return _row_to_approval(row)
