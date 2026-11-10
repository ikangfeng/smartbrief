"""LangGraph 工作流（第 24 节线性 / 第 25 节 A2A 条件循环 / 第 26 节短期记忆）。

```text
START → Planner → Scheduler → Reviewer ─passed→ END
                                   │
                                not passed
                                   ↓
                              (iteration < max ?)
                               ┌───┴───┐
                            yes        no(强制通过→END) ①
                               ↓
                            Rewriter ──循环回 Reviewer──┐
                                                       │
                                                       └────►
```
> ① 图中"已达上限强制通过"实为 **reviewer_node 内部完成**：不通过且
> `iteration += 1` 后 `>= max_iterations` 时，它直接返回 `passed=True +
> forced_pass=True` 并给草稿追加 ⚠️ 警告；条件边 route_after_review 只负责
> 二选一（passed / rewrite），不额外判断循环上限。

- Planner：根据 Topic 生成 Plan（步骤列表 + 依赖）；第 26 节先检索长期记忆作 Few-shot
- Scheduler：按依赖派发所有步骤给 ReAct Researcher / ReAct Writer 执行
- Reviewer（第 25 节）：审核草稿，输出 passed + review_comments（每轮追加 review_history）
- Rewriter（第 25 节）：按审核意见覆盖式修改草稿。循环上限 `max_iterations = 3`
  （**最多返工 2 次**，第 3 次审核仍不过才强制通过）

第 26 节短期记忆（Checkpointer）：`build_graph(checkpointer=...)` 编译时挂载
SqliteSaver（backend/checkpoints.db），task_id 作 thread_id；`run_task(resume=True)`
从最近一个图节点快照继续执行（后端重启后仍可恢复）。

持久化时机：图执行**完毕后**（run_task 内）把最终状态与 ReAct 日志统一写回 SQLite；
前端通过 /tasks/{task_id}/status 轮询任务完成后，即可查看完整的 Plan、A2A 循环与日志流。
"""
import asyncio
import logging

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from agents.gates import gate_plan_node, gate_report_node
from agents.planner import planner_node
from agents.rewriter import rewriter_node
from agents.reviewer import reviewer_node
from agents.scheduler import scheduler_node
from db import task_repository
from graph.state import AgentState
from memory import checkpointer as cp_mod
from memory import long_term_memory
from observability import tracing

logger = logging.getLogger(__name__)


def _traced_node(name: str, fn):
    """（维度一）给图节点包一层 node span：计时 + 状态 + 错误类别落 task_events。

    节点执行期间把该 span 设为"当前父 span"—— 节点内部产生的
    llm / tool 子 span 通过 contextvars 自动挂到它下面（瀑布图层级）。

    维度二：GraphInterrupt（人工审批闸门挂起）不是错误 ——
    特判为正常结束 span 后再向上抛，避免审批闸门在瀑布图里显示为红色错误。
    """
    async def wrapper(state: dict) -> dict:
        span = tracing.start_span("node", name)
        token = None if span.get("_noop") else tracing.set_current_span(span)
        try:
            result = await fn(state)
        except GraphInterrupt:
            tracing.finish_span(span)            # 审批挂起：正常结束，不记 error
            raise
        except Exception as exc:
            tracing.finish_span(span, status="error", error_class=type(exc).__name__)
            raise
        finally:
            if token is not None:
                tracing.reset_current_span(token)
        # 注意：不能在 try 里直接 return —— 那会跳过本行（else 不执行 return 路径）
        tracing.finish_span(span)
        return result

    wrapper.__name__ = getattr(fn, "__name__", name)
    return wrapper


def route_after_review(state: dict) -> str:
    """Reviewer 之后的**条件边**路由：通过 → 报告闸门；不通过 → Rewriter 返工。

    强制通过已在 reviewer_node 内部处理（不通过且达上限时返回 passed=True），
    所以这里只需要判断 passed 一个字段，逻辑保持单一。
    维度二：审核通过后先经 gate_report 人工终审（auto 模式直接放行），再 END。
    """
    return "passed" if state.get("passed") else "rewrite"


def route_after_plan_gate(state: dict) -> str:
    """计划闸门之后的路由：审批退回 → END（任务终态 rejected）；否则 → Scheduler。"""
    return "rejected" if state.get("approval_rejected") else "ok"


def build_graph(checkpointer=None):
    """构建并编译 LangGraph（第 25 节：带 Reviewer/Rewriter 条件循环；第 26 节：Checkpointer）。

    教学要点：
    - `add_conditional_edges("reviewer", 路由函数, 映射表)`：根据 state 动态决定下一步
    - 路由函数返回映射表里的 key（"passed"→END / "rewrite"→rewriter）
    - `add_edge("rewriter", "reviewer")`：返工后**循环回 Reviewer 再审核**（不重跑 Scheduler）
    - `compile(checkpointer=...)`：挂载短期记忆（SqliteSaver），task_id 作 thread_id，
      图节点执行完毕后自动写快照 → run_task(resume=True) 可断点续跑
    """
    graph = StateGraph(AgentState)
    # 维度一：所有节点经 _traced_node 包装后注册（节点级 span 自动落 task_events）
    graph.add_node("planner", _traced_node("planner", planner_node))          # 节点1：拆解计划
    graph.add_node("gate_plan", _traced_node("gate_plan", gate_plan_node))    # 节点2（维度二）：计划人工闸门
    graph.add_node("scheduler", _traced_node("scheduler", scheduler_node))    # 节点3：按依赖派发执行
    graph.add_node("reviewer", _traced_node("reviewer", reviewer_node))       # 节点4（25 节）：审核草稿
    graph.add_node("rewriter", _traced_node("rewriter", rewriter_node))       # 节点5（25 节）：按意见返工
    graph.add_node("gate_report", _traced_node("gate_report", gate_report_node))  # 节点6（维度二）：报告人工闸门
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "gate_plan")
    # 计划闸门：批准/改后批准 → Scheduler 执行；退回 → END（run_task 置 rejected）
    graph.add_conditional_edges(
        "gate_plan",
        route_after_plan_gate,
        {"ok": "scheduler", "rejected": END},
    )
    graph.add_edge("scheduler", "reviewer")          # 写完整稿 → 进入审核
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"passed": "gate_report", "rewrite": "rewriter"},  # 通过 → 报告闸门；不通过 → 返工
    )
    graph.add_edge("rewriter", "reviewer")           # 返工完 → 再次审核（条件循环）
    graph.add_edge("gate_report", END)               # 报告闸门：批准/退回都到 END（rejected 由 state 区分）
    return graph.compile(checkpointer=checkpointer)


def _initial_state(
    task_id: str,
    topic: str,
    approval_mode: str = "manual",
    max_tokens: int | None = None,
    max_duration_s: int | None = None,
) -> AgentState:
    """构造初始 AgentState（白板的"初始状态"）。

    所有字段都给默认值：plan 为空（等 Planner 生成）、
    日志为空、数据为空 —— 跑完图之后这些字段会被节点逐步填充。
    维度二：审批模式与执行预算随任务落库后由 run_task 读入初始状态。
    """
    return {
        "task_id": task_id,           # 任务 id（也是后续 checkpoint 的 thread_id）
        "topic": topic,               # 用户提交的研究主题
        "plan": [],                   # Planner 节点会写入步骤列表
        "current_step_index": 0,      # 初始进度 0
        "completed_steps": [],        # 初始无已完成步骤
        "react_logs": [],             # 初始无日志
        "research_data": {},          # 初始无研究数据
        "draft_content": "",          # 初始无草稿
        "final_report": None,         # 最终报告暂缺
        "error": None,                # 无异常
        # A2A 审核循环（第 25 节）
        "review_comments": [],        # 初始无审核意见
        "review_history": [],         # 初始无各轮审核记录
        "iteration": 0,               # 初始循环次数 0
        "max_iterations": 3,          # 循环上限 3 次（最多返工 2 次，第 3 次仍不过则强制通过）
        "passed": None,               # 尚未审核
        "forced_pass": False,         # 未强制通过
        # 长期记忆命中（第 26 节）：Planner 检索历史报告后填充
        "memory_hits": [],            # 初始无命中
        # 功能控制（维度二）：审批模式 + 预算护栏
        "approval_mode": approval_mode,
        "max_tokens": max_tokens,
        "max_duration_s": max_duration_s,
        "budget_exceeded": False,     # 未触发预算收口
        "approval_rejected": None,    # 未被审批退回
    }


async def _persist_logs(task_id: str, react_logs: list) -> None:
    """将图内生成的 ReAct 日志批量落库（task_logs 表）。

    教学要点：图执行期间 LLM 的 Thought/Action/Observation 都存在
    state["react_logs"] 里（内存），图跑完后统一写进 SQLite。
    这样前端轮询 /tasks/{id}/status 才能查到日志流。
    """
    for log in react_logs or []:
        task_repository.append_log(
            task_id,
            step_id=log.get("step_id", 0),
            log_type=log.get("type", "Thought"),
            content=str(log.get("content", "")),
        )


async def _remember_task(task_id: str, topic: str, final_report: str) -> None:
    """（第 26 节）任务成功后把 (topic, final_report) 写入长期记忆（容错，不阻断流程）。

    长期记忆由 ChromaDB 承担：embedding 走网络，故放进线程池；
    任何失败仅记 warning，绝不影响任务状态回写。
    """
    try:
        await asyncio.to_thread(long_term_memory.remember, topic, final_report, task_id)
    except Exception as exc:  # pragma: no cover - 真实异常路径
        logger.warning("任务 %s 写入长期记忆失败（忽略）：%s", task_id, exc)


# ---------------------------------------------------------------- 运行中任务注册表（维度二：人工终止）
# task_id → asyncio.Task：POST /tasks/{id}/cancel 据此 cancel() 协程，
# CancelledError 在 run_task 内特判（保持 cancelled 终态，不落入 failed）。
_RUNNING: dict[str, asyncio.Task] = {}


def register_running(task_id: str, task: asyncio.Task) -> None:
    """登记一个正在执行的工作流协程（API 层 create_task 后立即调用）。"""
    _RUNNING[task_id] = task


def cancel_running(task_id: str) -> bool:
    """取消正在执行的工作流协程；无登记（已结束/非本进程）返回 False。"""
    task = _RUNNING.get(task_id)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


async def run_task(
    task_id: str,
    topic: str,
    resume: bool = False,
    resume_value: dict | None = None,
) -> dict:
    """执行完整流程：运行图 → 落库日志与最终状态。

    流程示意：
        1. 构造初始 state（白板）；resume=True 且有快照时改为从断点继续
        2. graph.ainvoke(initial / None)：LangGraph 自动按边执行
           planner（生成计划）→ scheduler（派发 ReAct 执行）
           → reviewer（审核草稿；A2A 不通过则返工给 rewriter 覆盖式重写，最多返工 2 次）
        3. 把图内产生的 react_logs 批量落库（task_logs 表）
        4. 把最终状态写回 tasks 表（status=completed），
           含 A2A 审核结果字段：review_comments / review_history / iteration /
           max_iterations / passed / forced_pass 及第 26 节 memory_hits
        5. 任务成功后调用 remember() 写入长期记忆（memory_collection）
        6. 任一环节异常：把任务标记为 failed 并记录 error

    第 26 节短期记忆：图以 checkpointer=SqliteSaver(backend/checkpoints.db) 编译，
    task_id 作 thread_id —— 每次节点执行完自动落快照；resume=True 时从最近断点续跑
    （后端重启后仍可恢复，属"图执行到哪个节点"的框架层恢复）。

    :param task_id: 任务 id（由 API 层生成，也是 checkpoint 的 thread_id）
    :param topic: 用户提交的研究主题
    :param resume: True 表示从最近 checkpoint 恢复中断的任务（无快照则从头执行）
    :return: 最终 AgentState（dict）
    """
    # 维度二：审批模式与执行预算从任务记录读入初始状态
    # （auto 模式闸门节点自动放行，供 Harness 跑批；预算供 Scheduler 护栏检查）
    task_rec = task_repository.get_task(task_id) or {}
    initial = _initial_state(
        task_id, topic,
        approval_mode=task_rec.get("approval_mode") or "manual",
        max_tokens=task_rec.get("max_tokens"),
        max_duration_s=task_rec.get("max_duration_s"),
    )
    # 维度一：绑定可观测性上下文 —— 之后所有埋点（llm/tool/node span）
    # 都能经 contextvars 拿到本任务 id，无需改动任何函数签名
    tracing.bind_task(task_id)
    try:
        # 短期记忆 Checkpointer：以 async with 进入 SqliteSaver 连接生命周期
        # （新版 langgraph-checkpoint-sqlite 的 API），compile + invoke 全程持有连接，
        # 图每次节点执行后自动向 checkpoints.db 落快照（缺依赖时降级 MemorySaver）。
        async with cp_mod.open_checkpointer() as (cp, _kind):
            graph = build_graph(checkpointer=cp)
            config = {"configurable": {"thread_id": task_id}}
            if resume_value is not None:
                # 维度二：人工审批决策恢复 —— Command(resume=...) 让挂起的
                # interrupt() 返回决策 dict，闸门节点据此放行/覆盖/退回
                final = await graph.ainvoke(Command(resume=resume_value), config)
            elif resume and await cp_mod.async_has_checkpoint(cp, task_id):
                # 断点续跑：不传 input，LangGraph 从最近快照继续未完成的节点
                final = await graph.ainvoke(None, config)
            else:
                final = await graph.ainvoke(initial, config)          # 新任务从头跑
        # 维度二：图在人工审批闸门挂起 —— 任务状态已由闸门节点置为
        # waiting_plan_approval / waiting_report_approval；落库已有日志后返回，
        # 等待 POST /tasks/{id}/approval 决策后以 resume_value 恢复执行
        if final.get("__interrupt__"):
            await _persist_logs(task_id, final.get("react_logs", []))
            return final
        # 维度二：审批退回 —— 终态 rejected（原因来自审批意见）
        if final.get("approval_rejected"):
            await _persist_logs(task_id, final.get("react_logs", []))
            task_repository.update_task(
                task_id, status="rejected", error=final["approval_rejected"],
                plan=final.get("plan", []),
            )
            return final
        final_report = final.get("draft_content") or "（报告草稿为空）"
        await _persist_logs(task_id, final.get("react_logs", []))   # 日志落库
        task_repository.update_task(                  # 最终状态落库
            task_id,
            status="completed",
            plan=final.get("plan", []),
            current_step_index=final.get("current_step_index", 0),
            completed_steps=final.get("completed_steps", []),
            research_data=final.get("research_data", {}),
            draft_content=final.get("draft_content", ""),
            final_report=final_report,
            # A2A 审核循环结果（第 25 节）
            review_comments=final.get("review_comments", []),
            review_history=final.get("review_history", []),
            iteration=final.get("iteration", 0),
            max_iterations=final.get("max_iterations", 3),
            passed=bool(final.get("passed")),
            forced_pass=bool(final.get("forced_pass")),
            # 长期记忆命中（第 26 节）
            memory_hits=final.get("memory_hits", []),
            # 维度二：预算护栏触发标志（供前端展示"已强制收口"警告）
            budget_exceeded=1 if final.get("budget_exceeded") else 0,
        )
        # 成功 → 写入长期记忆（供后续相似任务 Few-shot）
        await _remember_task(task_id, topic, final_report)
        return final
    except asyncio.CancelledError:
        # 维度二：人工终止 —— 状态已由 cancel 接口置为 cancelled，保持终态并上抛，
        # 绝不能落入下面通用的 failed 分支
        logger.info("任务 %s 被人工终止（cancelled）", task_id)
        raise
    except GraphInterrupt:
        # 兜底：无 checkpointer 时审批挂起以异常形式上抛 —— 同样不是失败，
        # 状态已由闸门节点置为 waiting_*_approval，直接返回等待决策恢复
        logger.info("任务 %s 在人工审批闸门挂起（无快照路径）", task_id)
        return {}
    except Exception as exc:
        logger.exception("任务 %s 执行失败", task_id)
        task_repository.update_task(task_id, status="failed", error=str(exc))  # 失败也落库，前端可展示
        raise
    finally:
        _RUNNING.pop(task_id, None)             # 维度二：终态后从注册表摘除
