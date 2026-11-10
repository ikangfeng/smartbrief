import axios from 'axios'

/** 评测中心 API 封装（第 27 节；维度三：数据集 / Judge / 回归门禁） */

/** 单个用例的执行结果 */
export interface EvalCaseResult {
  case_index: number
  case_id?: string
  topic: string
  category?: string
  difficulty?: string
  /** standard（正常研究）/ guard（提示注入防护） */
  kind?: 'standard' | 'guard'
  keywords: string[]
  task_id: string
  success: boolean
  /** 耗时（秒）；失败用例为 null */
  duration_seconds: number | null
  /** ReAct 循环次数 = Action 日志条数 */
  react_loops: number
  /** 关键词覆盖率（0~1，命中数/总关键词数） */
  keyword_coverage: number
  /** 长期记忆命中条数 */
  memory_hits: number
  /** 是否因达循环上限被强制通过 */
  forced_pass: boolean
  /** LLM-as-Judge 质量分（1-5；未启用或失败为 null） */
  quality_score?: number | null
  quality_verdict?: string | null
  judge_rationale?: string | null
  judge_dimensions?: Record<string, number>
  judge_error?: string | null
  /** 防护用例是否通过（报告未泄露 negative_keywords） */
  guard_pass?: boolean
  guard_leaked?: string[]
  /** 任务 LLM 成本（美元，来自 task_events） */
  cost_usd?: number
  tokens_in?: number
  tokens_out?: number
  error: string | null
}

/** 评测指标汇总（实测值 + 目标值 + 达标判定） */
export interface EvalMetrics {
  summary: {
    total_cases: number
    success_cases: number
    failed_cases: number
    success_rate: number
    avg_duration_seconds: number
    p95_duration_seconds: number
    avg_react_loops: number
    keyword_coverage_rate: number
    memory_hit_rate: number
    avg_quality_score: number
    quality_scored_cases: number
    guard_case_count: number
    guard_pass_rate: number
    avg_cost_usd: number
    total_cost_usd: number
  }
  targets: Record<string, number>
  met: Record<string, boolean>
}

/** 回归门禁结论（与同数据集上一次 completed 评测对比） */
export interface EvalRegression {
  baseline_eval_id: string | null
  gate: 'pass' | 'fail' | 'skipped'
  blockers: string[]
  deltas: Record<string, number>
  rules: Record<string, number>
  message: string
}

/** 评测报告（详情接口返回） */
export interface EvalReport {
  eval_id: string
  /** running（执行中）/ completed / failed */
  status: 'running' | 'completed' | 'failed'
  total_cases: number
  completed_cases: number
  /** 本次评测使用的数据集 */
  dataset: string
  /** LLM-as-Judge 模型名（未启用为 null） */
  judge_model: string | null
  results: EvalCaseResult[]
  metrics: EvalMetrics
  /** 回归门禁结论（无基线时 gate=skipped） */
  regression: EvalRegression | null
  /** 本次评测配置（dataset / use_judge / baseline_eval_id / approval_mode） */
  config: Record<string, unknown>
  report_md: string
  error: string | null
  created_at: string
  updated_at: string
}

/** 评测报告摘要（列表项，不含大文本） */
export interface EvalReportSummary {
  eval_id: string
  status: EvalReport['status']
  total_cases: number
  completed_cases: number
  dataset: string
  judge_model: string | null
  error: string | null
  created_at: string
  updated_at: string
}

/** 评测列表响应 */
export interface EvalReportListResult {
  total: number
  page: number
  page_size: number
  items: EvalReportSummary[]
}

/** 可用数据集 */
export interface EvalDataset {
  name: string
  description: string
  case_count: number
}

/** 触发评测参数 */
export interface EvalRunOptions {
  dataset?: string
  use_judge?: boolean
  baseline_eval_id?: string | null
}

/** 触发评测响应 */
export interface EvalRunResult {
  eval_id: string
  status: string
  dataset: string
  use_judge: boolean
  total_cases: number
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 15000
})

/** 触发自动化评测（后台批量执行数据集用例，立即返回 eval_id） */
export async function runEvaluation(opts: EvalRunOptions = {}): Promise<EvalRunResult> {
  const { data } = await http.post('/evaluation/run', {
    dataset: opts.dataset ?? 'standard',
    use_judge: opts.use_judge ?? true,
    baseline_eval_id: opts.baseline_eval_id ?? null
  })
  return data as EvalRunResult
}

/** 可用评测数据集（下拉框数据源） */
export async function fetchEvalDatasets(): Promise<EvalDataset[]> {
  const { data } = await http.get('/evaluation/datasets')
  return (data as { datasets: EvalDataset[] }).datasets
}

/** 历史评测报告列表 */
export async function fetchEvalReports(params: { page?: number; page_size?: number } = {}): Promise<EvalReportListResult> {
  const { data } = await http.get('/evaluation/reports', { params })
  return data as EvalReportListResult
}

/** 评测报告详情（执行中返回进度，完成后返回结果与指标） */
export async function fetchEvalReport(evalId: string): Promise<EvalReport> {
  const { data } = await http.get(`/evaluation/reports/${evalId}`)
  return data as EvalReport
}

/** 删除历史评测报告（运行中的评测后端返回 400，前端提示） */
export async function deleteEvalReport(evalId: string): Promise<{ eval_id: string; deleted: boolean }> {
  const { data } = await http.delete(`/evaluation/reports/${evalId}`)
  return data as { eval_id: string; deleted: boolean }
}
