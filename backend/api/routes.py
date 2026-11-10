"""API 路由（第 24 节）：对话、工具管理、技能管理、任务管理、统计、健康检查。
第 26 节新增：知识库管理（上传/列表/检索）、长期记忆检索、任务断点恢复。
维度一新增：可观测性（task_events 查询/汇总/SSE 实时流）、断点信息卡。
"""
import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents import single_agent
from db import approval_repository
from db import evaluation_repository
from db import events_repository
from db import task_repository
from evaluation import dataset as eval_dataset
from evaluation import runner as eval_runner
from graph import workflow
from memory import checkpointer as cp_mod
from memory import long_term_memory
from mcp_client.mcp_client import mcp_manager
from observability import tracing
from rag import store as rag_store
from skills.skills_manager import manager as skill_manager
from tools import builtin_tools

router = APIRouter(prefix="/api/v1", tags=["chat"])


# ---------------------------------------------------------------- 请求/响应模型


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户提问")
    history: list = Field(default_factory=list, description="历史消息（OpenAI 格式）")


class GenerateRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=200, description="研究主题")
    # 维度二：审批模式（manual 双闸门人工审批 / auto 自动放行，Harness 跑批用）
    approval_mode: str = Field(default="manual", pattern="^(manual|auto)$", description="审批模式")
    # 维度二：执行预算护栏（None 不限；超限强制收口并标注 budget_exceeded）
    max_tokens: int | None = Field(default=None, ge=1, description="token 预算上限")
    max_duration_s: int | None = Field(default=None, ge=1, description="时长预算上限（秒）")


class ApprovalDecisionRequest(BaseModel):
    """人工审批决策（维度二）：批准 / 改后批准 / 退回。"""
    action: str = Field(..., pattern="^(approve|edit|reject)$", description="决策动作")
    version: int = Field(..., ge=1, description="读取审批记录时的 version（乐观锁，不匹配返回 409）")
    approver: str = Field(default="人工", max_length=64, description="审批人")
    comment: str = Field(default="", max_length=500, description="审批意见")
    edited_payload: dict | list | str | None = Field(default=None, description="改后批准的修订内容（计划 JSON 或报告 Markdown）")


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="检索文本")
    k: int = Field(default=5, ge=1, le=20, description="返回条数")
    category: str | None = Field(default=None, description="限定分类：template / terminology / upload / report")


class BuiltinToggleRequest(BaseModel):
    enabled: bool = Field(..., description="开关状态：true 开启 / false 关闭")


class EvaluationRunRequest(BaseModel):
    """触发自动化评测的参数（维度三）：数据集 / Judge / 基线。"""
    dataset: str = Field(default="standard", description="数据集名：standard | smoke")
    use_judge: bool = Field(default=True, description="是否启用 LLM-as-Judge 质量评分")
    baseline_eval_id: str | None = Field(
        default=None, description="回归基线评测 id；为空则自动取同数据集最近一次 completed"
    )


class MCPServerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Server 名称")
    url: str = Field(..., min_length=1, description="MCP Server URL（HTTP JSON-RPC 端点）")
    description: str = Field(default="", max_length=200, description="描述")
    enabled: bool = Field(default=True, description="是否启用")
    transport: str = Field(
        default="http", pattern="^(http|sse)$",
        description="传输方式：http（Streamable HTTP）| sse（HTTP+SSE）",
    )


class MCPServerUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Server 名称")
    url: str = Field(..., min_length=1, description="MCP Server URL（HTTP JSON-RPC 端点）")
    description: str = Field(default="", max_length=200, description="描述")
    enabled: bool = Field(default=True, description="是否启用")
    transport: str = Field(
        default="http", pattern="^(http|sse)$",
        description="传输方式：http（Streamable HTTP）| sse（HTTP+SSE）",
    )


class MCPServerEnabledRequest(BaseModel):
    enabled: bool = Field(..., description="开关状态：true 开启 / false 关闭")


# ---------------------------------------------------------------- 技能请求/响应模型


class SkillCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$", description="技能唯一标识")
    display_name: str = Field(..., min_length=1, max_length=64, description="中文展示名")
    description: str = Field(default="", max_length=500, description="技能描述")
    category: str = Field(default="analysis", pattern="^(search|analysis|writing|review|other)$", description="分类")
    version: str = Field(default="1.0.0", max_length=32, description="版本号")
    skill_md: str = Field(default="", description="skill.md Markdown 正文（可选，缺省生成模板）")
    enabled: bool = Field(default=True, description="开关：true 开启 / false 关闭")


class SkillUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=64, description="中文展示名")
    description: str | None = Field(default=None, max_length=500, description="技能描述")
    category: str | None = Field(default=None, pattern="^(search|analysis|writing|review|other)$", description="分类")
    version: str | None = Field(default=None, max_length=32, description="版本号")
    skill_md: str | None = Field(default=None, description="skill.md Markdown 正文")
    enabled: bool | None = Field(default=None, description="开关：true 开启 / false 关闭")


class SkillEnabledRequest(BaseModel):
    enabled: bool = Field(..., description="开关状态：true 开启 / false 关闭")


class SkillRunRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="用户请求")
    context: dict = Field(default_factory=dict, description="上下文（可选）")


# ---------------------------------------------------------------- 健康检查


@router.get("/health")
async def health():
    """健康检查。"""
    return {
        "status": "ok",
        "service": "smartbrief-backend",
        "mcp_server": mcp_manager.is_running,
    }


# ---------------------------------------------------------------- 对话


@router.post("/chat")
async def chat(req: ChatRequest):
    """单智能体对话：提示词触发内置工具 / MCP 工具调用（仅注入已开启的工具）。"""
    try:
        result = await single_agent.run(req.message, req.history)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"对话失败：{exc}") from exc


# ---------------------------------------------------------------- 工具列表（汇总）


@router.get("/tools")
async def list_tools():
    """全部工具汇总：内置（含开关状态）+ MCP（按 Server 展开，含开关状态）。"""
    builtin = builtin_tools.list_tools()
    mcp_tools = []
    for server in mcp_manager.list_servers():
        for t in server.get("tools", []):
            mcp_tools.append(
                {
                    "name": t["name"],
                    "type": "mcp",
                    "description": t.get("description", ""),
                    "enabled": server["enabled"],
                    "server": server["name"],
                }
            )
    return {"tools": builtin + mcp_tools}


# ---------------------------------------------------------------- 内置工具管理


@router.get("/tools/builtin")
async def list_builtin_tools():
    """内置工具列表（含开关状态）。"""
    return {"tools": builtin_tools.list_tools()}


@router.put("/tools/builtin/{name}")
async def toggle_builtin_tool(name: str, req: BuiltinToggleRequest):
    """开关内置工具（关闭后不再注入 LLM，但保留在注册表中）。"""
    tool = builtin_tools.set_enabled(name, req.enabled)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"未知内置工具：{name}")
    return {"name": tool.name, "enabled": tool.enabled}


# ---------------------------------------------------------------- MCP 工具管理


@router.get("/tools/mcp")
async def list_mcp_servers():
    """MCP Server 列表（含工具、开关状态、最近错误）。"""
    return {"servers": mcp_manager.list_servers()}


@router.post("/tools/mcp")
async def add_mcp_server(req: MCPServerRequest):
    """添加自定义 MCP Server（支持 http / sse 传输，保存后自动尝试发现工具）。"""
    try:
        server = await mcp_manager.add_server(
            req.name, req.url, req.description, req.enabled, req.transport
        )
        return {"server": server}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/tools/mcp/{server_id}")
async def update_mcp_server(server_id: str, req: MCPServerUpdateRequest):
    """编辑自定义 MCP Server（可切换传输方式，重新发现工具）。"""
    try:
        server = await mcp_manager.update_server(
            server_id, req.name, req.url, req.description, req.enabled, req.transport
        )
        return {"server": server}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/tools/mcp/{server_id}")
async def delete_mcp_server(server_id: str):
    """删除自定义 MCP Server（内置 Server 不可删除）。"""
    try:
        await mcp_manager.delete_server(server_id)
        return {"deleted": server_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/tools/mcp/{server_id}/enabled")
async def toggle_mcp_server(server_id: str, req: MCPServerEnabledRequest):
    """开关 MCP Server（内置与自定义均支持）。"""
    try:
        server = await mcp_manager.set_enabled(server_id, req.enabled)
        return {"server": server}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tools/mcp/{server_id}/refresh")
async def refresh_mcp_server(server_id: str):
    """手动刷新某 Server 的工具列表。"""
    try:
        tools = await mcp_manager.refresh_tools(server_id)
        return {"tools": tools}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- 工具统计


@router.get("/tools/stats")
async def tool_stats():
    """工具调用统计（内存）：次数 / 成功 / 失败。"""
    return {"stats": single_agent.get_stats()}


# ---------------------------------------------------------------- 技能管理（第 23 节）


@router.get("/skills")
async def list_skills():
    """技能列表（含 builtin / source / skill_md）。"""
    return {"skills": skill_manager.list_skills()}


@router.post("/skills")
async def create_skill(req: SkillCreateRequest):
    """新增自定义技能（表单 + skill.md 正文）。"""
    try:
        return {"skill": skill_manager.create_skill(**req.model_dump())}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/skills/{name}")
async def update_skill(name: str, req: SkillUpdateRequest):
    """编辑自定义技能（内置技能不可修改）。"""
    try:
        return {"skill": skill_manager.update_skill(name, **req.model_dump(exclude_unset=True))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/skills/{name}")
async def delete_skill(name: str):
    """删除技能（内置技能返回 400：不可删除）。"""
    try:
        skill_manager.delete_skill(name)
        return {"deleted": name}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/skills/{name}/enabled")
async def toggle_skill(name: str, req: SkillEnabledRequest):
    """技能开关（内置 / 自定义均可）。"""
    try:
        return {"skill": skill_manager.set_enabled(name, req.enabled)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/skills/import")
async def import_skill(file: UploadFile = File(...)):
    """导入 .zip 技能包（自动解析 skill.md，校验路径安全与大小）。"""
    data = await file.read()
    try:
        return {"skill": skill_manager.import_skill(data)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/skills/{name}/run")
async def run_skill(name: str, req: SkillRunRequest):
    """试运行技能：内置 mock 直接返回；自定义技能按 skill.md / skill.py 执行。"""
    try:
        output = await skill_manager.run_skill(name, req.query, req.context)
        return {"name": name, "output": output.model_dump()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/distill-skill")
async def distill_skill(task_id: str):
    """把已完成任务的报告一键沉淀为写作技能（报告 → Skill 闭环）。

    沉淀资格：任务已 completed 且有报告内容（审核通过才到终态，
    即「沉淀 = 被审核认证过的写作方法」）。提炼出的技能分类为 writing，
    持久化到 skills_pool/，后续撰写任务由 Writer 自动挂载复用。
    """
    from skills.distill import distill_skill_from_task

    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task["status"] != "completed" or not (task.get("final_report") or task.get("draft_content")):
        raise HTTPException(status_code=400, detail="仅已完成的报告可沉淀为技能")
    try:
        skill = await distill_skill_from_task(task)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"技能提炼失败：{exc}") from exc
    return {"skill": skill, "source_task_id": task_id}


@router.post("/tasks/{task_id}/score")
async def score_task_report(task_id: str):
    """对单篇报告做五维质量评分（复用评测体系的 Judge，评审模型独立解耦）。

    定位：Reviewer 管"能不能出厂"（流程内），本接口管"这篇到底多好"（用户侧），
    评测中心管"系统有没有退化"（开发侧）——三层各司其职。
    """
    from evaluation import judge

    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    report = task.get("final_report") or task.get("draft_content")
    if not report:
        raise HTTPException(status_code=400, detail="任务尚无报告内容，无法评分")
    result = await judge.judge_report(task.get("topic") or "", report)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=f"评分失败：{result['error']}")
    result["judge_model"] = judge.get_model_name()
    return result


# ---------------------------------------------------------------- 任务管理（第 24 节）


@router.post("/generate")
async def generate_task(req: GenerateRequest):
    """提交任务：创建任务记录并异步执行 LangGraph 工作流，立即返回 task_id。

    维度二：approval_mode=manual 时工作流会在计划/报告两道人工闸门挂起
    （任务状态 waiting_plan_approval / waiting_report_approval）；
    auto 直接跑完全程（Harness 跑批用）。预算参数超限会触发强制收口。
    """
    task_id = f"task_{uuid4().hex[:8]}"
    topic = req.topic.strip()
    # 先写入任务初始记录（running），再后台执行（前端轮询 status 可见）
    task_repository.create_task(
        task_id, topic,
        approval_mode=req.approval_mode,
        max_tokens=req.max_tokens,
        max_duration_s=req.max_duration_s,
    )
    # 登记运行中协程（维度二：cancel 接口据此终止执行）
    workflow.register_running(task_id, asyncio.create_task(workflow.run_task(task_id, topic)))
    return {"task_id": task_id, "status": "running", "topic": topic, "approval_mode": req.approval_mode}


@router.get("/tasks")
async def list_tasks(
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数"),
    status: str | None = Query(default=None, description="按状态过滤：running / paused / completed / failed"),
    keyword: str | None = Query(default=None, max_length=200, description="按主题模糊搜索（报告中心搜索框）"),
    order: str = Query("desc", pattern="^(asc|desc)$", description="时间排序：desc 最新在前 / asc 最久在前"),
):
    """任务列表（分页 + 状态过滤 + 主题搜索 + 时间排序），第 27 节「报告中心」数据源。"""
    items, total = task_repository.list_tasks(
        status=status,
        keyword=keyword,
        order=order,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/tasks/{task_id}/status")
async def task_status(task_id: str):
    """任务详情：Plan 进度 + 完整 ReAct 日志 + A2A 审核结果（第 25 节，从 SQLite 读取）。

    A2A 返回字段：review_history（各轮意见）/ review_comments / iteration /
    max_iterations / passed / forced_pass，是前端详情页审核卡片的直接数据源。
    """
    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    task["react_logs"] = task_repository.get_logs(task_id)
    return task


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务（级联删除其 ReAct 日志），任务不存在返回 404。"""
    if not task_repository.delete_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {"task_id": task_id, "deleted": True}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    """恢复暂停/失败的任务（第 26 节短期记忆）：从最近 checkpoint 断点续跑。

    适用：任务因后端重启/中断被自动标记为 paused（或 failed 但有节点快照）。
    已完成任务返回 400（无需恢复）；无快照时自动从头执行兜底。

    维度一：恢复动作留痕 —— tasks 表 resume_count+1 / last_resume_at，
    并向日志流写一条 System 日志（前端断点信息卡与日志流均可见）。
    """
    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task["status"] == "completed":
        raise HTTPException(status_code=400, detail="任务已完成，无需恢复")
    has_snapshot = cp_mod.has_checkpoint(task_id)
    # 重置为 running（曾在 failed 的任务也可重跑），清除旧错误信息
    task_repository.update_task(task_id, status="running", error=None)
    task_repository.mark_resumed(task_id)            # 维度一：恢复计数 + 时间
    task_repository.append_log(
        task_id, step_id=0, log_type="System",
        content=f"人工触发恢复执行（{'从最近断点续跑' if has_snapshot else '无快照，从头执行'}）",
    )
    workflow.register_running(
        task_id, asyncio.create_task(workflow.run_task(task_id, task["topic"], resume=True))
    )
    return {"task_id": task_id, "status": "running", "resumed": True}


# ---------------------------------------------------------------- 人工审批与终止（维度二）


# 可被人工终止的任务状态（终态不可取消）
_CANCELLABLE = ("running", "paused", "waiting_plan_approval", "waiting_report_approval")


@router.get("/tasks/{task_id}/approval")
async def get_current_approval(task_id: str):
    """当前待审批记录（审批卡片数据源）；无待审批返回 approval=null。

    同时返回 payload_hash 供前端展示"所见即所批"指纹，
    以及历史记录数（审批留痕入口）。
    """
    if task_repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {"approval": approval_repository.get_pending(task_id)}


@router.get("/tasks/{task_id}/approvals")
async def list_task_approvals(task_id: str):
    """任务的全部审批记录（留痕历史：每道闸门的快照/哈希/决策人/意见）。"""
    if task_repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {"approvals": approval_repository.list_approvals(task_id)}


@router.post("/tasks/{task_id}/approval")
async def decide_approval(task_id: str, req: ApprovalDecisionRequest):
    """提交人工审批决策：批准 / 改后批准 / 退回，随后从闸门断点恢复执行。

    并发保护（乐观锁）：version 与当前记录不一致、或记录已被他人决策，
    返回 409 —— 前端提示刷新后重试，避免"两人同时审批互相覆盖"。

    决策后任务状态从 waiting_*_approval 回到 running，
    工作流以 Command(resume=决策) 恢复挂起的图节点。
    """
    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    pending = approval_repository.get_pending(task_id)
    if pending is None:
        raise HTTPException(status_code=400, detail="当前没有待审批的闸门")
    decided = approval_repository.decide(
        pending["id"], req.action, req.version,
        approver=req.approver, comment=req.comment, edited_payload=req.edited_payload,
    )
    if decided is None:
        raise HTTPException(status_code=409, detail="审批记录已被更新，请刷新后重试（乐观锁冲突）")

    # 恢复图执行：状态回 running，interrupt() 将返回本决策
    task_repository.update_task(task_id, status="running", error=None)
    action_label = {"approve": "批准", "edit": "改后批准", "reject": "退回"}[req.action]
    task_repository.append_log(
        task_id, step_id=0, log_type="System",
        content=f"人工审批决策：{action_label}（{req.approver}）"
                + (f"，意见：{req.comment}" if req.comment else ""),
    )
    decision = {
        "action": req.action,
        "approver": req.approver,
        "comment": req.comment,
        "edited_payload": req.edited_payload,
    }
    workflow.register_running(
        task_id,
        asyncio.create_task(workflow.run_task(task_id, task["topic"], resume_value=decision)),
    )
    return {"task_id": task_id, "status": "running", "approval": decided}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """人工终止任务（维度二）：取消运行中的协程并置终态 cancelled。

    幂等：已终态任务返回 400。等待审批中的任务同样可终止
    （审批闸门挂起时协程已结束，仅置状态即可）。
    """
    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task["status"] not in _CANCELLABLE:
        raise HTTPException(status_code=400, detail=f"任务已是终态（{task['status']}），无需终止")
    stopped = workflow.cancel_running(task_id)          # 有协程则取消（触发 CancelledError）
    task_repository.update_task(task_id, status="cancelled", error="人工终止")
    task_repository.append_log(
        task_id, step_id=0, log_type="System",
        content="任务被人工终止" + ("（执行中协程已取消）" if stopped else "（无运行中协程，仅置状态）"),
    )
    return {"task_id": task_id, "status": "cancelled", "stopped": stopped}


# ---------------------------------------------------------------- 可观测性（维度一）


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    """任务的全部 span（node/llm/tool，按开始时间升序）—— Flight Recorder 瀑布图数据源。"""
    if task_repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {"events": events_repository.list_events(task_id)}


@router.get("/tasks/{task_id}/events/summary")
async def task_events_summary(task_id: str):
    """任务成本/性能聚合（token 合计、成本合计、按模型/工具分组）—— 成本卡数据源。"""
    if task_repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return events_repository.summarize_events(task_id)


@router.get("/tasks/{task_id}/events/stream")
async def task_events_stream(task_id: str):
    """SSE 实时推送 span 事件（span_start / span_end），前端 Flight Recorder 实时刷新。

    连接建立后先回放库中已有 span（补历史），再持续推送新事件；
    任务到达终态（completed/failed）且事件队列排空后发送 done 事件并关闭。
    """
    if task_repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    async def gen():
        # 1) 回放：库中已落库的 span（断线重连/中途打开页面也能看到完整历史）
        for span in events_repository.list_events(task_id):
            yield f"data: {json.dumps({'kind': 'span_end', 'span': span}, ensure_ascii=False)}\n\n"
        # 2) 实时：订阅总线，推送新事件；任务终态后再等一个心跳周期收尾巴
        queue = tracing.event_bus.subscribe(task_id)
        try:
            while True:
                task = task_repository.get_task(task_id)
                finished = (task or {}).get("status") in ("completed", "failed")
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    if finished:
                        yield f"data: {json.dumps({'kind': 'done'})}\n\n"
                        break
                    yield ": heartbeat\n\n"          # SSE 注释行作心跳，防代理断连
        finally:
            tracing.event_bus.unsubscribe(task_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}/checkpoint")
async def task_checkpoint(task_id: str):
    """断点信息卡：最新 checkpoint 的元数据与状态摘要（无快照返回 has_checkpoint=false）。

    返回 checkpoint_id / 快照时间 / 已执行 super-step 数 / 计划进度 /
    A2A 轮次 / 草稿字数，以及任务的 resume_count / last_resume_at。
    """
    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    info = await asyncio.to_thread(cp_mod.latest_checkpoint_info, task_id)
    return {
        "task_id": task_id,
        "has_checkpoint": info is not None,
        "checkpoint": info,
        "resume_count": task.get("resume_count") or 0,
        "last_resume_at": task.get("last_resume_at"),
    }


# ---------------------------------------------------------------- 知识库管理（第 26 节 RAG）


@router.get("/knowledge/documents")
async def list_knowledge_documents():
    """RAG 知识库文档列表（前端"知识库"页数据源）。

    教学链路：ChromaDB 里同一份文档会切成多个 chunk 分条存储，
    这里经 rag_store.list_documents 按 (category, source) **聚合**成一个
    "文件"条目（展示分类/块数/首段摘要），否则前端看到的是碎片。
    asyncio.to_thread：list_documents 内部走 ChromaDB 本地文件 IO，
    属阻塞调用，丢进线程池避免卡住 FastAPI 事件循环。
    """
    try:
        docs = await asyncio.to_thread(rag_store.list_documents)
        return {"documents": docs}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取知识库失败：{exc}") from exc


@router.delete("/knowledge/document")
async def delete_knowledge_document(
    source: str = Query(..., min_length=1, description="文档来源（source）"),
    category: str = Query(..., pattern="^(template|terminology|upload)$", description="文档分类"),
):
    """删除知识库文档（按 category + source 精确定位一个"文件"）。

    教学要点：删除单位是**文件级**而非单条 chunk —— 一个文件对应多个
    md5 分块，store.delete_document 先按 `$and` 多条件过滤查出全部
    相关 chunk id，再批量删除，保证不残留孤儿块。
    """
    try:
        deleted = await asyncio.to_thread(rag_store.delete_document, source, category)
        return {"deleted": deleted}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除文档失败：{exc}") from exc


@router.get("/knowledge/document")
async def get_knowledge_document_detail(
    source: str = Query(..., min_length=1, description="文档来源（source）"),
    category: str = Query(..., pattern="^(template|terminology|upload)$", description="文档分类"),
):
    """知识库文档详情：返回指定 (category, source) 的所有 chunk（前端"查看"弹窗数据源）。

    教学链路：列表页只展示每个文件的概况；点"查看"后前端调本接口，
    后端按 (category, source) 双条件（ChromaDB `$and` 语法）过滤出该文件
    的全部分块，按 chunk 内容排序返回 —— 便于学员理解"分块→入库→聚合读取"闭环。
    """
    try:
        chunks = await asyncio.to_thread(rag_store.get_document_chunks, source, category)
        if not chunks:
            raise HTTPException(status_code=404, detail="文档不存在")
        return {"source": source, "category": category, "chunks": chunks}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取文档详情失败：{exc}") from exc


@router.post("/knowledge/documents")
async def upload_knowledge_document(
    file: UploadFile = File(...),
    category: str = Form("upload", description="分类：upload（默认）/ template / terminology"),
):
    """上传文档到 RAG 知识库（教学链路：存盘 → 分格式解析 → 递归分块 → 向量化入库）。

    与 zhiqida-rag 一致的处理管线（rag_store.ingest_file 内部）：
      1. 原始字节先落盘到 data/uploads/（保留源文件，便于溯源与重试）
      2. DocumentLoader 按扩展名分发：txt/md 读文本、docx 提段落、
         pdf 每页一个 Document、xlsx 每 sheet 一个 Document
      3. RecursiveChunker 按段落/句子边界切块（512 字 + 64 字重叠）
      4. ChromaVectorStore 用 bge-m3 向量化 + md5 稳定 ID 去重后入库
    上传后立即可在知识库列表/检索测试里看到效果（无需重启后端）。
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    filename = file.filename or "unnamed.txt"
    try:
        result = await asyncio.to_thread(
            rag_store.ingest_file, content, filename, category or "upload"
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文档入库失败：{exc}") from exc


@router.post("/knowledge/search")
async def search_knowledge(req: KnowledgeSearchRequest):
    """RAG 语义检索测试（知识库页"检索测试"用）。

    教学要点：这不是关键词匹配，而是**向量检索** ——
    把用户 query 经同一嵌入模型向量化后，在 ChromaDB 里按 cosine 相似度
    找最相近的 chunk（含模板/术语/上传三类，可传 category 过滤）。
    可选阈值过滤见 rag_store.search_rag_terms（Writer 注入术语用）。
    """
    try:
        hits = await asyncio.to_thread(
            rag_store.search_rag, req.query, req.k, req.category
        )
        return {"query": req.query, "hits": hits}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"知识库检索失败：{exc}") from exc


# ---------------------------------------------------------------- 长期记忆（第 26 节）


@router.get("/memory/search")
async def search_memory(
    q: str = Query(..., min_length=1, max_length=200, description="检索文本（历史报告主题/内容）"),
    k: int = Query(5, ge=1, le=20, description="返回条数"),
):
    """检索长期记忆（历史任务报告，内部供 TaskDetail 回溯/调试）。

    教学要点：长期记忆与 RAG 知识库是**两个独立的 ChromaDB 集合**——
    rag_collection 存预置模板/术语/上传文档，memory_collection 只存
    成功任务的 (topic, final_report)。本接口就是 memory_collection 的
    语义检索入口，与 Planner 规划前调用的 recall() 走同一条检索链路。
    """
    try:
        hits = await asyncio.to_thread(long_term_memory.search_memory, q, k)
        return {"query": q, "hits": hits}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"长期记忆检索失败：{exc}") from exc


@router.get("/memory/status")
async def memory_status():
    """长期记忆开关状态 + 已存储报告数量（知识库页"记忆开关"卡数据源）。

    教学要点：开关关闭时接口仍返回 enabled=false 且计数置 0 ——
    计数依赖开关（关闭时不做 ChromaDB 查询），避免学员误解"关闭=清空"。
    """
    try:
        from rag.store import get_store, MEMORY_COLLECTION
        store = get_store(None, MEMORY_COLLECTION)
        report_count = store.count({"type": "report"}) if long_term_memory.is_enabled() else 0
    except Exception:
        report_count = 0
    return {"enabled": long_term_memory.is_enabled(), "count": report_count}


@router.post("/memory/toggle")
async def memory_toggle(req: BuiltinToggleRequest):
    """开启 / 关闭长期记忆（运行时开关）。

    教学要点：这是**逻辑开关**而非数据删除 —— set_enabled 只翻转进程内
    标志，关闭后 remember/recall 短路返回；已存入 memory_collection 的
    历史报告原样保留，重新开启即可继续使用。
    """
    await asyncio.to_thread(long_term_memory.set_enabled, req.enabled)
    return {"enabled": req.enabled, "message": f"长期记忆已{'开启' if req.enabled else '关闭'}"}


# ---------------------------------------------------------------- 自动化评测（第 27 节）


@router.post("/evaluation/run")
async def trigger_evaluation(req: EvaluationRunRequest | None = None):
    """触发自动化评测：后台批量执行数据集用例，立即返回 eval_id。

    教学要点：返回后前端用 eval_id 轮询 `GET /evaluation/reports/{eval_id}`，
    该接口在评测执行中即可返回进度（completed_cases / total_cases），
    完成后返回八项指标（JSON）+ 回归门禁结论 + Markdown 可视化总结。
    已有评测在运行时返回 400（并发保护）；数据集非法返回 400。
    """
    opts = req or EvaluationRunRequest()
    try:
        eval_id = eval_runner.start_evaluation(
            dataset=opts.dataset,
            use_judge=opts.use_judge,
            baseline_eval_id=opts.baseline_eval_id,
        )
    except ValueError as exc:  # 数据集不存在 / 文件损坏
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:  # 并发保护
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "eval_id": eval_id,
        "status": "running",
        "dataset": opts.dataset,
        "use_judge": opts.use_judge,
        "total_cases": eval_runner.case_count(opts.dataset),
    }


@router.get("/evaluation/datasets")
async def list_evaluation_datasets():
    """可用评测数据集（评测中心下拉框：名称 / 说明 / 用例数）。"""
    return {"datasets": eval_dataset.list_datasets()}


@router.get("/evaluation/reports")
async def list_evaluation_reports(
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数"),
):
    """历史评测报告列表（评测中心「历史评测」表格数据源，按时间倒序）。"""
    items, total = evaluation_repository.list_reports(
        limit=page_size, offset=(page - 1) * page_size
    )
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/evaluation/reports/{eval_id}")
async def get_evaluation_report(eval_id: str):
    """评测报告详情：进度（执行中）或完整结果（已结束）。

    教学要点：这是评测中心的核心数据源 ——
    执行中返回 status=running + 进度；结束后返回
    results（逐用例明细）/ metrics（五维指标 + 达标判定）/ report_md（Markdown 总结）。
    """
    report = evaluation_repository.get(eval_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"评测报告不存在：{eval_id}")
    return report


@router.delete("/evaluation/reports/{eval_id}")
async def delete_evaluation_report(eval_id: str):
    """删除历史评测报告（评测中心「删除」操作）。

    教学要点：评测无子表（results/metrics 都存本行），删除即整行移除；
    运行中的评测返回 400 —— 后台 `_execute` 仍会继续写进度，删掉记录后
    更新会落到 0 行、前端轮询也会 404，容易造成混乱，故禁止删除。
    """
    report = evaluation_repository.get(eval_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"评测报告不存在：{eval_id}")
    if report["status"] == "running":
        raise HTTPException(status_code=400, detail="评测正在执行中，暂不可删除")
    evaluation_repository.delete(eval_id)
    return {"eval_id": eval_id, "deleted": True}


# ---------------------------------------------------------------- 任务报告（第 27 节最终完善）


@router.get("/tasks/{task_id}/report")
async def task_report(task_id: str):
    """获取任务最终报告（Markdown）与执行摘要（「报告展示」页数据源）。

    教学要点：报告 = 任务成功后的 final_report（草稿即最终报告），
    附带执行摘要供前端展示：总耗时（created_at → updated_at）、
    ReAct 行动次数（Action 日志条数）、长期记忆命中数。
    任务未完成 / 不存在时返回 404（报告页跳转兜底）。
    """
    task = task_repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task["status"] != "completed":
        raise HTTPException(status_code=404, detail="任务尚未完成，暂无可用报告")
    content = task.get("final_report") or task.get("draft_content") or ""
    logs = task_repository.get_logs(task_id)
    react_loops = sum(1 for log in logs if log.get("log_type") == "Action")
    created_at = task.get("created_at") or ""
    updated_at = task.get("updated_at") or ""
    total_time = _elapsed_seconds(created_at, updated_at)
    return {
        "task_id": task_id,
        "title": task.get("topic", ""),
        "content_markdown": content,
        "charts": [],  # Analyst 图表 Agent 属后续扩展（PRD 展望），当前无图
        "executive_summary": {
            "total_time": total_time,
            "react_loops": react_loops,
            "memory_hit_count": len(task.get("memory_hits") or []),
        },
    }


def _elapsed_seconds(created_at: str, updated_at: str) -> float | None:
    """任务耗时（秒）：created_at → updated_at（SQLite 本地时间格式 %Y-%m-%d %H:%M:%S）。

    解析失败返回 None（前端显示 “—”），不阻断报告展示。
    """
    from datetime import datetime

    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        start = datetime.strptime(created_at.strip(), fmt)
        end = datetime.strptime(updated_at.strip(), fmt)
        return round(max(0.0, (end - start).total_seconds()), 1)
    except (ValueError, AttributeError):
        return None
