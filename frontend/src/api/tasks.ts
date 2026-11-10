import axios from 'axios'

/** 任务状态（维度二新增：waiting_plan_approval / waiting_report_approval 待审批、cancelled 已终止、rejected 审批退回） */
export type TaskStatusName =
  | 'running' | 'paused' | 'completed' | 'failed'
  | 'waiting_plan_approval' | 'waiting_report_approval'
  | 'cancelled' | 'rejected'

/** 任务摘要（列表项） */
export interface TaskSummary {
  task_id: string
  topic: string
  status: TaskStatusName
  plan: Array<{ id: number; agent: string; task: string; depends_on: number[] }>
  current_step_index: number
  completed_steps: number[]
  created_at: string
  updated_at: string
}

/** ReAct 日志条目 */
export interface ReactLog {
  id: number
  task_id: string
  step_id: number
  log_type: 'Thought' | 'Action' | 'Observation'
  content: string
  created_at: string
}

/** 单轮审核记录（review_history 元素） */
export interface ReviewRound {
  round: number
  passed: boolean
  comments: string[]
}

/** 长期记忆命中（第 26 节）：Planner 规划前检索到的相似历史报告 */
export interface MemoryHit {
  topic: string
  /** 历史报告的结构（# 开头章节标题） */
  structure?: string[]
  snippet?: string
  /** 相似度分数（0~1，越高越相似） */
  score: number
  task_id: string
  created_at: string
}

/** 任务详情（status 接口返回） */
export interface TaskStatus extends TaskSummary {
  research_data: Record<string, unknown>
  draft_content: string
  final_report: string | null
  error: string | null
  react_logs: ReactLog[]
  // A2A 审核循环（第 25 节）
  review_comments: string[]
  review_history?: ReviewRound[]
  iteration: number
  max_iterations: number
  passed: boolean
  forced_pass: boolean
  // 长期记忆命中（第 26 节）
  memory_hits: MemoryHit[]
  // 功能控制（维度二）
  approval_mode: 'manual' | 'auto'
  max_tokens: number | null
  max_duration_s: number | null
  budget_exceeded: number
}

/** 任务列表响应 */
export interface TaskListResult {
  total: number
  page: number
  page_size: number
  items: TaskSummary[]
}

/** 提交任务响应 */
export interface GenerateResult {
  task_id: string
  status: string
  topic: string
}

/** 任务报告（第 27 节「报告展示」页数据源） */
export interface TaskReport {
  task_id: string
  title: string
  content_markdown: string
  /** 图表图片（Analyst 图表 Agent 属后续扩展，当前为空数组） */
  charts: Array<{ title: string; image_base64: string }>
  executive_summary: {
    /** 总耗时（秒，created_at → updated_at；无法解析时为 null） */
    total_time: number | null
    /** ReAct 行动次数 */
    react_loops: number
    /** 长期记忆命中条数 */
    memory_hit_count: number
  }
}

// ---------------------------------------------------------------- 可观测性（维度一）

/** 可观测性 span（task_events 一行：node 图节点 / llm 模型调用 / tool 工具调用） */
export interface TaskEvent {
  id: number
  task_id: string
  span_id: string
  parent_span_id: string | null
  span_type: 'node' | 'llm' | 'tool'
  name: string
  status: 'ok' | 'error'
  start_ts: number
  end_ts: number
  duration_ms: number
  model: string | null
  tokens_in: number | null
  tokens_out: number | null
  cost_usd: number | null
  tool_name: string | null
  args_hash: string | null
  retry_count: number | null
  error_class: string | null
  detail: Record<string, unknown>
  created_at: string
}

/** 成本/性能聚合（events/summary 接口返回，Flight Recorder 成本卡数据源） */
export interface EventSummary {
  span_count: number
  node_count: number
  llm_calls: number
  tool_calls: number
  tokens_in: number
  tokens_out: number
  cost_usd: number
  error_count: number
  total_duration_ms: number
  models: Record<string, { calls: number; tokens_in: number; tokens_out: number; cost_usd: number }>
  tools: Record<string, { calls: number; retries: number; errors: number }>
}

/** 断点信息卡（checkpoint 接口返回） */
export interface CheckpointInfo {
  task_id: string
  has_checkpoint: boolean
  checkpoint: {
    checkpoint_id: string | null
    created_at: string | null
    step: number
    plan_steps: number
    completed_steps: number
    completed_step_ids: number[]
    iteration: number
    draft_chars: number
  } | null
  resume_count: number
  last_resume_at: string | null
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 15000
})

/** 提交任务选项（维度二：审批模式 + 预算护栏） */
export interface SubmitOptions {
  approval_mode?: 'manual' | 'auto'
  max_tokens?: number | null
  max_duration_s?: number | null
}

/** 提交研究任务（返回 task_id，立即返回） */
export async function submitTask(topic: string, opts: SubmitOptions = {}): Promise<GenerateResult> {
  const { data } = await http.post('/generate', { topic, ...opts })
  return data as GenerateResult
}

// ---------------------------------------------------------------- 功能控制（维度二）

/** 审批记录（task_approvals 一行） */
export interface Approval {
  id: number
  task_id: string
  gate: 'plan' | 'report'
  status: 'pending' | 'approved' | 'edited' | 'rejected'
  payload_snapshot: unknown           // plan 闸门为步骤数组；report 闸门为草稿文本
  payload_hash: string
  version: number
  approver: string | null
  comment: string | null
  edited_payload: unknown
  decided_at: string | null
  created_at: string
}

/** 当前待审批记录（无待审批时 approval 为 null） */
export async function fetchCurrentApproval(taskId: string): Promise<Approval | null> {
  const { data } = await http.get(`/tasks/${taskId}/approval`)
  return data.approval as Approval | null
}

/** 全部审批记录（留痕历史） */
export async function fetchApprovals(taskId: string): Promise<Approval[]> {
  const { data } = await http.get(`/tasks/${taskId}/approvals`)
  return data.approvals as Approval[]
}

/** 提交审批决策（批准 / 改后批准 / 退回）；version 乐观锁冲突时后端返回 409 */
export async function decideApproval(
  taskId: string,
  payload: {
    action: 'approve' | 'edit' | 'reject'
    version: number
    approver?: string
    comment?: string
    edited_payload?: unknown
  }
): Promise<{ status: string; approval: Approval }> {
  const { data } = await http.post(`/tasks/${taskId}/approval`, payload)
  return data as { status: string; approval: Approval }
}

/** 人工终止任务（置终态 cancelled） */
export async function cancelTask(taskId: string): Promise<{ status: string; stopped: boolean }> {
  const { data } = await http.post(`/tasks/${taskId}/cancel`)
  return data as { status: string; stopped: boolean }
}

/** 任务列表（分页 + 状态过滤 + 主题搜索 + 时间排序） */
export async function fetchTasks(params: {
  page?: number
  page_size?: number
  status?: string
  /** 按主题模糊搜索（报告中心搜索框，第 27 节） */
  keyword?: string
  /** 时间排序：desc 最新在前 / asc 最久在前（报告中心，第 27 节） */
  order?: 'asc' | 'desc'
} = {}): Promise<TaskListResult> {
  const { data } = await http.get('/tasks', { params })
  return data as TaskListResult
}

/** 任务详情：Plan 进度 + ReAct 日志 */
export async function fetchTaskStatus(taskId: string): Promise<TaskStatus> {
  const { data } = await http.get(`/tasks/${taskId}/status`)
  return data as TaskStatus
}

/** 删除任务（级联删除其 ReAct 日志） */
export async function deleteTask(taskId: string): Promise<{ task_id: string; deleted: boolean }> {
  const { data } = await http.delete(`/tasks/${taskId}`)
  return data as { task_id: string; deleted: boolean }
}

/** 恢复任务（第 26 节短期记忆）：暂停/失败的任务从最近 checkpoint 断点续跑 */
export async function resumeTask(taskId: string): Promise<{ task_id: string; status: string; resumed: boolean }> {
  const { data } = await http.post(`/tasks/${taskId}/resume`)
  return data as { task_id: string; status: string; resumed: boolean }
}

/** 任务报告（第 27 节）：最终报告 Markdown + 执行摘要（仅已完成任务可获取） */
export async function fetchTaskReport(taskId: string): Promise<TaskReport> {
  const { data } = await http.get(`/tasks/${taskId}/report`)
  return data as TaskReport
}

// ---------------------------------------------------------------- 可观测性（维度一）

/** 任务的全部 span（Flight Recorder 瀑布图数据源，按开始时间升序） */
export async function fetchTaskEvents(taskId: string): Promise<TaskEvent[]> {
  const { data } = await http.get(`/tasks/${taskId}/events`)
  return data.events as TaskEvent[]
}

/** 任务成本/性能聚合（成本卡数据源） */
export async function fetchTaskEventsSummary(taskId: string): Promise<EventSummary> {
  const { data } = await http.get(`/tasks/${taskId}/events/summary`)
  return data as EventSummary
}

/** 断点信息卡：最新 checkpoint 摘要 + 恢复计数 */
export async function fetchTaskCheckpoint(taskId: string): Promise<CheckpointInfo> {
  const { data } = await http.get(`/tasks/${taskId}/checkpoint`)
  return data as CheckpointInfo
}

/** SSE 实时订阅 span 事件（任务执行中 Flight Recorder 实时刷新）。
 *  返回 EventSource；调用方负责 close()。onEvent 收到 span_start / span_end / done。 */
export function streamTaskEvents(
  taskId: string,
  onEvent: (evt: { kind: string; span?: TaskEvent }) => void,
  onError?: () => void
): EventSource {
  const es = new EventSource(`/api/v1/tasks/${taskId}/events/stream`)
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data))
    } catch {
      /* 忽略心跳/坏帧 */
    }
  }
  if (onError) es.onerror = onError
  return es
}

/** 单篇报告质量评分（五维，Judge 模型独立于工作者模型；LLM 评审较慢，放宽超时） */
export interface ReportScore {
  score: number | null
  dimensions: Record<string, number>
  verdict: string
  rationale: string
  judge_model: string
}
export async function scoreTaskReport(taskId: string): Promise<ReportScore> {
  const { data } = await http.post(`/tasks/${taskId}/score`, null, { timeout: 120000 })
  return data as ReportScore
}
