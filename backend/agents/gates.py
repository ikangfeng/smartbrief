"""人工审批闸门节点（维度二）：gate_plan（计划闸门）+ gate_report（报告闸门）。

设计要点（LangGraph interrupt 人工在环）：
1. approval_mode=auto 时节点直接放行（Harness 跑批/回归门禁不阻塞）。
2. manual 模式：先把待审内容即时落库（task_approvals 快照 + tasks.plan），
   再 `interrupt()` 挂起图 —— 任务状态置为 waiting_plan_approval /
   waiting_report_approval，前端审批卡片据此渲染。
3. 人工决策（POST /tasks/{id}/approval）后，run_task 以
   `Command(resume=decision)` 恢复图执行，interrupt() 返回决策 dict：
   - approve：原样放行
   - edit：用 edited_payload 覆盖（计划换计划 / 报告换草稿）
   - reject：写入 approval_rejected，条件边路由到 END，任务终态 rejected

幂等性陷阱：interrupt 恢复时**整个节点从头重跑**，所以 interrupt() 之前的
副作用（建审批记录 / 更新任务状态 / 写日志）必须幂等 ——
这里以"是否已存在该闸门记录"为判据，重跑时静默跳过。
"""
import logging

from langgraph.types import interrupt

from agents.planner import parse_plan
from db import approval_repository, task_repository

logger = logging.getLogger(__name__)

# 待审内容长度保护：快照截断上限（草稿很长时审批卡片不至于拉爆）
SNAPSHOT_LIMIT = 20000


def _is_manual(state: dict) -> bool:
    """仅 manual 模式启用人工闸门（auto 供 Harness 跑批直接放行）。"""
    return state.get("approval_mode", "manual") == "manual"


def _ensure_pending(task_id: str, gate: str, payload) -> None:
    """（幂等）确保存在该闸门的 pending 审批记录，并把待审内容即时落库。

    interrupt 恢复时节点从头重跑：此时记录已存在（且已被决策），
    直接跳过全部副作用，只让 interrupt() 返回恢复值。
    """
    if approval_repository.get_current(task_id, gate) is not None:
        return
    approval_repository.create_pending(task_id, gate, payload)
    status = "waiting_plan_approval" if gate == "plan" else "waiting_report_approval"
    update = {"status": status}
    if gate == "plan":
        update["plan"] = payload                     # 计划即时落库，供人审与前端看板展示
    task_repository.update_task(task_id, **update)
    label = "执行计划" if gate == "plan" else "报告草稿"
    task_repository.append_log(
        task_id, step_id=0, log_type="System",
        content=f"{label}已提交人工审批（gate_{gate}），等待决策：批准 / 改后批准 / 退回",
    )
    logger.info("任务 %s 进入人工审批闸门 gate_%s", task_id, gate)


def _apply_decision(task_id: str, gate: str, decision: dict, logs: list) -> dict:
    """统一处理审批决策：approve 放行 / edit 覆盖 / reject 退回。

    :return: state 增量（reject 时含 approval_rejected，由条件边路由到 END）
    """
    action = (decision or {}).get("action", "approve")
    comment = (decision or {}).get("comment") or ""
    approver = (decision or {}).get("approver") or "人工"
    label = "计划" if gate == "plan" else "报告"

    if action == "reject":
        reason = f"{label}审批被{approver}退回" + (f"：{comment}" if comment else "")
        logs.append({"type": "System", "content": reason, "step_id": 0})
        return {"approval_rejected": reason, "react_logs": logs}

    if action == "edit":
        edited = (decision or {}).get("edited_payload")
        if gate == "plan" and edited:
            # 人工修订后的计划走同一套解析/校验（白名单 agent + 截断）
            import json as _json

            try:
                raw = edited if isinstance(edited, list) else _json.loads(str(edited))
                new_plan = parse_plan(_json.dumps(raw, ensure_ascii=False))
            except Exception:
                new_plan = []
            if new_plan:
                logs.append(
                    {"type": "System", "content": f"计划经{approver}修订后批准（{len(new_plan)} 步）", "step_id": 0}
                )
                return {"plan": new_plan, "react_logs": logs}
            logs.append({"type": "System", "content": "人工修订的计划非法，按原计划放行", "step_id": 0})
        elif gate == "report" and isinstance(edited, str) and edited.strip():
            logs.append(
                {"type": "System", "content": f"报告经{approver}修订后批准（{len(edited)} 字）", "step_id": 0}
            )
            return {"draft_content": edited, "react_logs": logs}
        # edit 但无有效修订内容 → 按批准处理（兜底）

    logs.append({"type": "System", "content": f"{label}审批通过（{approver}）", "step_id": 0})
    return {"react_logs": logs}


async def gate_plan_node(state: dict) -> dict:
    """计划闸门：Planner 生成计划后挂起，人工审批通过才进入 Scheduler 执行。"""
    if not _is_manual(state):
        return {}
    task_id = state["task_id"]
    plan = state.get("plan", [])
    _ensure_pending(task_id, "plan", plan)
    decision = interrupt({"gate": "plan", "plan": plan})   # 挂起；恢复时返回决策
    return _apply_decision(task_id, "plan", decision, list(state.get("react_logs", [])))


async def gate_report_node(state: dict) -> dict:
    """报告闸门：Reviewer 审核通过后挂起，人工终审通过才输出最终报告。"""
    if not _is_manual(state):
        return {}
    task_id = state["task_id"]
    draft = state.get("draft_content", "")
    _ensure_pending(task_id, "report", draft[:SNAPSHOT_LIMIT])
    decision = interrupt({"gate": "report"})               # 挂起；恢复时返回决策
    return _apply_decision(task_id, "report", decision, list(state.get("react_logs", [])))
