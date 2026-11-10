"""Scheduler（第 24 节）：按依赖关系派发 plan 步骤给对应执行 Agent。

每一轮取出所有「依赖已满足」的步骤，**同轮并发执行**（asyncio.gather）：
先并发跑 researcher 批次、汇总研究数据后，再并发跑 writer 批次
—— 保证 writer 能看到本轮全部研究产出，同时把联网搜索的墙钟等待压缩近一半。
跨轮仍按依赖串行解锁（多 Agent 审核返工的条件循环由第 25 节 workflow.py 层的条件边实现）。
"""
import asyncio
from typing import List, Dict

from agents.react_researcher import react_researcher_run
from agents.react_writer import collect_sources, react_writer_run
from db import events_repository

# 第 25 节容错：单个 Agent 步骤执行超时上限（超过则跳过该步骤，不让工作流卡死）。
# Researcher 走联网搜索、单步较快；Writer 需一次性生成整篇章节/报告、耗时明显更长。
# 若 Writer 沿用同一较短超时会被误掐断（草稿退化为占位符，后续 A2A 返工极易跑题），故单独放宽。
# 超时上限需覆盖一个完整 ReAct 循环：最多 MAX_REACT_STEPS(3) 次搜索 + 1 次 fetch，
# 每次 MCP 调用本身可达 MCP_CALL_TIMEOUT_SECONDS(30s)（Tavily 走代理约 8-10s/次），
# 故 30s 会在搜索成功后把整步掐掉、sources 全丢（报告 0 引用的根因）。
AGENT_STEP_TIMEOUT_SECONDS = 150    # researcher 步骤超时上限（3 搜索 + 1 fetch + 决策 LLM 调用）
WRITER_STEP_TIMEOUT_SECONDS = 120   # writer 步骤超时上限（撰写整稿需要更长时间）


def _budget_breach(state: dict) -> str | None:
    """（维度二）预算护栏检查：基于维度一 task_events 实时汇总判断是否超限。

    两类预算（均在提交任务时可选设置，None 表示不限）：
    - max_tokens：LLM token 用量（tokens_in + tokens_out 合计）上限
    - max_duration_s：墙钟时长上限（首个 span 起点 → 现在的秒数）

    :return: 超限原因文本；未超限/未设预算返回 None
    """
    max_tokens = state.get("max_tokens")
    max_duration_s = state.get("max_duration_s")
    if not max_tokens and not max_duration_s:
        return None                                  # 未设预算：不查库，零开销
    task_id = state.get("task_id")
    try:
        summary = events_repository.summarize_events(task_id)
    except Exception:
        return None                                  # 观测数据不可用时放行（不阻断主流程）
    if max_tokens:
        used = (summary.get("tokens_in") or 0) + (summary.get("tokens_out") or 0)
        if used >= max_tokens:
            return f"token 用量 {used} 已达预算上限 {max_tokens}"
    if max_duration_s:
        # 墙钟起点：首个 span 的 start_ts；无任何 span 时用 0 跳过（还没开始跑不可能超时）
        if summary.get("span_count"):
            # summarize 只给总时长（首 span 起点→末 span 终点），
            # 但护栏关心的是"首 span 起点→现在"，这里用总时长近似足够（末 span 即最近活动）
            elapsed = (summary.get("total_duration_ms") or 0) / 1000
            if elapsed >= max_duration_s:
                return f"执行时长 {elapsed:.0f}s 已达预算上限 {max_duration_s}s"
    return None


def _ready_steps(plan: List[Dict], completed: List[int]) -> List[Dict]:
    """返回所有"依赖已满足、尚未完成"的步骤（按 id 升序，保证确定性）。

    就绪判定规则：
    - 步骤的 id 不在 completed 中（还没执行过）
    - 步骤 depends_on 里的每个依赖 id 都在 completed 中（依赖全完成）

    为什么按 id 升序？保证多次调度结果一致（确定性），
    避免 LLM 计划里步骤乱序导致执行顺序不稳定。
    """
    completed_set = set(completed)               # 转 set 加速"依赖是否已完成"判断
    ready = []
    for step in plan:
        if step["id"] in completed_set:
            continue                             # 已完成的步骤跳过
        # all(...)：depends_on 为空列表时返回 True（无依赖 → 立即就绪）
        if all(dep in completed_set for dep in step.get("depends_on", [])):
            ready.append(step)
    return sorted(ready, key=lambda s: s["id"])  # 按 id 升序，保证确定性


def _append_log(logs: List[Dict], step_id: int, log_type: str, content: str) -> None:
    """追加日志（统一入口，保证每条日志都带 step_id 归类）。"""
    logs.append({"type": log_type, "content": content, "step_id": step_id})


async def _run_research_step(step: Dict, logs: List[Dict]) -> tuple:
    """执行单个研究步骤（ReAct 搜索循环），返回 (step_id, 研究结果)。

    容错：整个步骤限时，超时则返回降级结果（observations=[] + skipped 标记），
    流程不中断、不重试，依赖本步骤的 writer 会收到"数据缺失"信号。
    设计为纯函数式协程（不原地改 research_data），供 gather 并发调用。
    """
    step_id = step["id"]
    try:
        result = await asyncio.wait_for(
            react_researcher_run(step.get("task", ""), step_id, logs),
            timeout=AGENT_STEP_TIMEOUT_SECONDS,
        )
        return step_id, result
    except asyncio.TimeoutError:
        _append_log(
            logs, step_id, "Observation",
            f"步骤 {step_id} 执行超时（>{AGENT_STEP_TIMEOUT_SECONDS}s），已跳过",
        )
        return step_id, {
            "observations": [],
            "skipped": True,
            "error": f"timeout>{AGENT_STEP_TIMEOUT_SECONDS}s",
        }


async def _run_writer_step(step: Dict, logs: List[Dict], research_data: Dict) -> tuple:
    """执行单个撰写步骤（带研究数据生成章节草稿），返回 (step_id, 章节文本)。

    容错：超时降级为占位章节（占位文案与 Rewriter 的"草稿为空/仅占位则重写"
    约定呼应，改动需两边同步），保证后续拼接 / 审核不因缺章节而崩。
    research_data 并发期间只读，writer 之间互不干扰。
    """
    step_id = step["id"]
    try:
        section = await asyncio.wait_for(
            react_writer_run(step.get("task", ""), step_id, logs, research_data),
            timeout=WRITER_STEP_TIMEOUT_SECONDS,
        )
        return step_id, section
    except asyncio.TimeoutError:
        _append_log(
            logs, step_id, "Observation",
            f"步骤 {step_id} 执行超时（>{WRITER_STEP_TIMEOUT_SECONDS}s），已跳过",
        )
        return step_id, f"（步骤 {step_id} 撰写超时，内容待补充）"


async def scheduler_node(state: dict) -> dict:
    """LangGraph 节点：调度执行所有可执行步骤。

    核心思想：**反复"取就绪步骤 → 派发执行 → 更新完成状态"**，
    直到没有可执行步骤为止（类似 while 有活干就派活）。

    派发规则：
    - researcher 步骤 → 调 react_researcher_run（ReAct 搜索循环）
    - writer 步骤 → 调 react_writer_run（基于研究数据撰写章节）
    每完成一个步骤，就把它的 id 加入 completed_steps，
    下一轮就绪判定会解锁依赖它的后续步骤。

    :param state: AgentState（含 plan / completed_steps / research_data / draft_content）
    :return: state 增量（completed_steps、research_data、draft_content、current_step_index、react_logs）
    """
    plan = state.get("plan", [])
    completed: List[int] = list(state.get("completed_steps", []))   # 已完成步骤（会逐步累加）
    logs: List[Dict] = list(state.get("react_logs", []))            # 日志流（原地追加）
    research_data: Dict = dict(state.get("research_data", {}))      # 研究数据（逐步填充）
    drafts: List[str] = []                                          # writer 步骤产出的章节（暂存）

    # 维度二：预算护栏 —— 每轮派发前检查 token/时长预算；
    # 超限后强制收口：不再派发任何新步骤，用已有素材直接收尾（标注 budget_exceeded）。
    budget_breach: str | None = None

    # 逐轮派发：每轮取所有就绪步骤，同轮内 gather 并发执行
    # （研究批次先跑完再跑撰写批次，保证 writer 看到本轮全部研究数据）
    pending = True
    while pending:
        ready = _ready_steps(plan, completed)     # 1. 找出本轮可执行的步骤
        if not ready:
            pending = False                       # 没有就绪步骤 → 全部完成，退出循环
            break
        # ---------- 维度二：派发前预算检查（超限即收口，不再花一分钱）----------
        breach = _budget_breach(state)
        if breach:
            budget_breach = breach
            _append_log(
                logs, 0, "System",
                f"预算护栏触发：{breach}。剩余 {len(ready)} 个步骤不再派发，"
                f"已强制收口（budget_exceeded），基于已有素材收尾。",
            )
            break
        researchers = [s for s in ready if s.get("agent", "researcher") == "researcher"]
        writers = [s for s in ready if s.get("agent", "researcher") != "researcher"]
        for step in ready:                        # 2. 派发日志（实际执行并发进行）
            _append_log(
                logs, step["id"], "Thought",
                f"调度：步骤 {step['id']} 派发给 {step.get('agent', 'researcher')}",
            )
        if researchers:
            # 3a. 并发执行研究批次；gather 保序（按 step id 升序），结果统一归档
            results = await asyncio.gather(*(_run_research_step(s, logs) for s in researchers))
            for step_id, result in results:
                research_data[f"step_{step_id}"] = result       # 研究结果按步骤号归档
                completed.append(step_id)                       # 标记完成，解锁后续依赖
                _append_log(logs, step_id, "Observation", f"步骤 {step_id} 完成")
        if writers:
            # 3b. 并发执行撰写批次（共享只读 research_data）；章节按 id 序收集，
            # 与串行版的拼接顺序保持一致（确定性）
            sections = await asyncio.gather(*(_run_writer_step(s, logs, research_data) for s in writers))
            for step_id, section in sections:
                drafts.append(section)                # 章节暂存，最后统一拼接
                completed.append(step_id)
                _append_log(logs, step_id, "Observation", f"步骤 {step_id} 完成")

    # 组装草稿：writer 步骤的章节按执行顺序拼接成完整 Markdown
    draft_content = "\n\n".join(drafts)
    if not draft_content:
        # 没有 writer 步骤（或全部失败）时给出占位说明
        draft_content = "（无 writer 步骤生成内容）"
    elif sources := collect_sources(research_data):
        # 报告质量增强：文末自动追加「参考资料」章节（编号由 collect_sources 统一分配，
        # 与 Writer 行内引用 [n] 一致；<a id="ref-n"> 锚点供前端引用跳转）
        ref_lines = ["## 参考资料", ""]
        ref_lines += [
            f'{s["id"]}. <a id="ref-{s["id"]}"></a>[{s["title"]}]({s["url"]})'
            for s in sources
        ]
        draft_content += "\n\n" + "\n".join(ref_lines)

    return {
        "completed_steps": completed,             # 已完成步骤 id 列表
        "research_data": research_data,           # 各步骤研究结果（供前端/Writer 引用）
        "draft_content": draft_content,           # 拼接后的报告草稿
        "current_step_index": len(completed),     # 进度 = 已完成步骤数
        "react_logs": logs,                       # 日志流
        "budget_exceeded": bool(budget_breach),   # 维度二：预算护栏触发标志（随状态落库）
    }
