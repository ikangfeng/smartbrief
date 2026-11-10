<script setup lang="ts">
/** 评测中心（第 27 节）：运行评测 → 进度条 → 五维雷达图 + 指标卡 + 用例明细 + 历史对比 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import * as echarts from 'echarts'
import {
  runEvaluation,
  fetchEvalReports,
  fetchEvalReport,
  fetchEvalDatasets,
  deleteEvalReport,
  type EvalReport,
  type EvalReportSummary,
  type EvalCaseResult,
  type EvalDataset
} from '../api/evaluation'

// ---------------- 状态 ----------------

const running = ref(false)          // 当前是否有评测在执行（含后台）
const starting = ref(false)         // 正在触发评测
const reports = ref<Awaited<ReturnType<typeof fetchEvalReports>>['items']>([])
const selectedId = ref('')          // 当前选中的评测 id
const report = ref<EvalReport | null>(null)
const loading = ref(false)
const pollTimer = ref<number | null>(null)
// 维度三：数据集选择 + LLM-as-Judge 开关
const datasets = ref<EvalDataset[]>([])
const selectedDataset = ref('standard')
const useJudge = ref(true)

async function loadDatasets() {
  try {
    datasets.value = await fetchEvalDatasets()
    const exists = datasets.value.some((d) => d.name === selectedDataset.value)
    if (!exists && datasets.value.length) selectedDataset.value = datasets.value[0].name
  } catch {
    ElMessage.error('获取评测数据集失败，请检查后端服务')
  }
}

async function loadList() {
  try {
    const result = await fetchEvalReports({ page: 1, page_size: 20 })
    reports.value = result.items
    // 列表里有执行中的评测 → 自动选中并轮询
    const active = result.items.find((r) => r.status === 'running')
    if (active) {
      running.value = true
      select(active.eval_id)
    } else if (result.items.length && !report.value) {
      // 默认展示最近一次评测
      select(result.items[0].eval_id)
    }
  } catch {
    ElMessage.error('获取评测历史失败，请检查后端服务')
  }
}

async function loadDetail(evalId: string) {
  loading.value = true
  try {
    report.value = await fetchEvalReport(evalId)
    running.value = report.value.status === 'running'
    if (running.value) startPolling()
    else stopPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '获取评测详情失败')
  } finally {
    loading.value = false
  }
}

function select(evalId: string) {
  selectedId.value = evalId
  void loadDetail(evalId)
}

function onRowClick(row: { eval_id: string }) {
  select(row.eval_id)
}

// ---------------- 删除历史评测（第 27 节） ----------------

async function confirmDelete(row: EvalReportSummary) {
  try {
    await ElMessageBox.confirm(
      `确定删除评测报告 ${row.eval_id}（${row.completed_cases}/${row.total_cases}）吗？该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await deleteEvalReport(row.eval_id)
    ElMessage.success(`已删除评测报告 ${row.eval_id}`)
    // 当前选中的正是被删除的评测 → 清空选中，列表刷新后自动展示最新一条
    if (selectedId.value === row.eval_id) {
      selectedId.value = ''
      report.value = null
      stopPolling()
      running.value = false
    }
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败，请检查后端服务')
  }
}

// ---------------- 轮询（执行中 2s 刷新） ----------------

function startPolling() {
  stopPolling()
  pollTimer.value = window.setInterval(() => {
    if (selectedId.value) void loadDetail(selectedId.value)
  }, 2000)
}

function stopPolling() {
  if (pollTimer.value !== null) {
    window.clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

onMounted(() => {
  loadDatasets()
  loadList()
})

onBeforeUnmount(() => {
  stopPolling()
  radarChart.value?.dispose()
})

// ---------------- 运行评测 ----------------

async function onRun() {
  if (running.value || starting.value) return
  starting.value = true
  try {
    const result = await runEvaluation({
      dataset: selectedDataset.value,
      use_judge: useJudge.value
    })
    ElMessage.success(
      `评测已启动：${result.eval_id}（数据集 ${result.dataset}，共 ${result.total_cases} 个用例）`
    )
    running.value = true
    select(result.eval_id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '评测启动失败，请检查后端服务')
  } finally {
    starting.value = false
  }
}

// ---------------- 派生数据 ----------------

const summary = computed(() => report.value?.metrics?.summary)

/** 指标展示卡（实测值 + 目标值 + 达标标签；null 表示该指标不适用） */
const metricCards = computed(() => {
  const s = summary.value
  if (!s) return []
  const pct = (v: number) => `${(v * 100).toFixed(1)}%`
  const target = report.value!.metrics.targets
  const met = report.value!.metrics.met
  const qualityEnabled = (s.quality_scored_cases || 0) > 0
  const guardApplicable = (s.guard_case_count || 0) > 0
  return [
    { key: 'success_rate', label: '任务成功率', value: pct(s.success_rate), target: pct(target.success_rate), met: met.success_rate },
    { key: 'avg_duration_seconds', label: '平均执行耗时', value: `${s.avg_duration_seconds.toFixed(1)}s`, target: `${target.avg_duration_seconds.toFixed(1)}s`, met: met.avg_duration_seconds },
    { key: 'p95_duration_seconds', label: 'P95 耗时', value: `${s.p95_duration_seconds.toFixed(1)}s`, target: '—', met: true },
    { key: 'avg_react_loops', label: '平均 ReAct 循环', value: `${s.avg_react_loops.toFixed(1)} 次`, target: `${target.avg_react_loops.toFixed(1)} 次`, met: met.avg_react_loops },
    { key: 'keyword_coverage_rate', label: '关键词覆盖率', value: pct(s.keyword_coverage_rate), target: pct(target.keyword_coverage_rate), met: met.keyword_coverage_rate },
    { key: 'memory_hit_rate', label: '记忆命中率', value: pct(s.memory_hit_rate), target: pct(target.memory_hit_rate), met: met.memory_hit_rate },
    {
      key: 'avg_quality_score',
      label: '报告质量分（Judge）',
      value: qualityEnabled ? `${s.avg_quality_score.toFixed(2)} 分` : '未启用',
      target: qualityEnabled ? `${target.avg_quality_score.toFixed(1)} 分` : '—',
      met: qualityEnabled ? met.avg_quality_score : null
    },
    {
      key: 'guard_pass_rate',
      label: '注入防护通过率',
      value: guardApplicable ? pct(s.guard_pass_rate) : '不适用',
      target: guardApplicable ? pct(target.guard_pass_rate) : '—',
      met: guardApplicable ? met.guard_pass_rate : null
    },
    {
      key: 'avg_cost_usd',
      label: '单任务平均成本',
      value: `$${(s.avg_cost_usd ?? 0).toFixed(4)}`,
      target: `$${target.avg_cost_usd.toFixed(4)}`,
      met: met.avg_cost_usd
    }
  ]
})

/** 雷达图数据：多维度统一归一化到 0~100（质量分 / 防护率在适用时追加） */
const radarOption = computed(() => {
  const s = summary.value
  if (!s) return null
  const durationScore = s.avg_duration_seconds > 0
    ? Math.max(0, Math.min(100, (1 - s.avg_duration_seconds / 120) * 100))
    : 0
  const loopScore = s.avg_react_loops > 0
    ? Math.max(0, Math.min(100, (1 - s.avg_react_loops / 8) * 100))
    : 0
  const indicators = [
    { name: '成功率', max: 100 },
    { name: '关键词覆盖率', max: 100 },
    { name: '记忆命中率', max: 100 },
    { name: '耗时达标分', max: 100 },
    { name: '循环达标分', max: 100 }
  ]
  const values = [
    +(s.success_rate * 100).toFixed(1),
    +(s.keyword_coverage_rate * 100).toFixed(1),
    +(s.memory_hit_rate * 100).toFixed(1),
    +durationScore.toFixed(1),
    +loopScore.toFixed(1)
  ]
  if ((s.quality_scored_cases || 0) > 0) {
    indicators.push({ name: '报告质量分', max: 100 })
    values.push(Math.min(100, +((s.avg_quality_score / 5) * 100).toFixed(1)))
  }
  if ((s.guard_case_count || 0) > 0) {
    indicators.push({ name: '注入防护', max: 100 })
    values.push(+(s.guard_pass_rate * 100).toFixed(1))
  }
  return {
    tooltip: {},
    radar: { indicator: indicators, radius: '65%' },
    series: [
      {
        type: 'radar',
        data: [
          {
            value: values,
            name: '评测指标',
            areaStyle: { opacity: 0.25 }
          }
        ]
      }
    ]
  }
})

const radarChart = ref<echarts.ECharts | null>(null)
const radarEl = ref<HTMLElement>()

watch(radarOption, async () => {
  if (!radarOption.value) return
  await nextTick()
  if (!radarEl.value) return
  radarChart.value ??= echarts.init(radarEl.value)
  radarChart.value.setOption(radarOption.value)
})

/** Markdown 报告（report_md）渲染为富文本 */
const reportMdHtml = computed(() => {
  const md = report.value?.report_md ?? ''
  if (!md) return ''
  const raw = marked.parse(md, { async: false }) as string
  return DOMPurify.sanitize(raw)
})

const showMd = ref(true)
const showCases = ref(true)

/** 回归门禁横幅信息（pass 绿 / fail 红 / skipped 灰） */
const regressionInfo = computed(() => {
  const r = report.value?.regression
  if (!r) return null
  const meta = {
    pass: { type: 'success' as const, title: '回归门禁通过' },
    fail: { type: 'error' as const, title: '回归门禁未通过' },
    skipped: { type: 'info' as const, title: '无基线，跳过回归判定' }
  }[r.gate] ?? { type: 'info' as const, title: '回归门禁' }
  const deltas = r.deltas || {}
  const deltaText = [
    `成功率 ${formatDelta(deltas.success_rate)}`,
    `关键词覆盖率 ${formatDelta(deltas.keyword_coverage_rate)}`,
    r.baseline_eval_id ? `基线 ${r.baseline_eval_id}` : ''
  ].filter(Boolean).join(' · ')
  return { ...meta, blockers: r.blockers || [], deltaText }
})

function formatDelta(v: number | undefined) {
  if (v == null) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${(v * 100).toFixed(1)}pp`
}

function caseStatusType(r: EvalCaseResult) {
  if (r.success) return 'success' as const
  return 'danger' as const
}

function caseStatusLabel(r: EvalCaseResult) {
  if (r.success) return '成功'
  return '失败'
}

function coveragePercent(v: number) {
  return `${(v * 100).toFixed(1)}%`
}

function qualityText(r: EvalCaseResult) {
  return r.quality_score == null ? '—' : `${r.quality_score.toFixed(2)} 分`
}

function costText(r: EvalCaseResult) {
  return `$${(r.cost_usd ?? 0).toFixed(4)}`
}

function durationText(r: EvalCaseResult) {
  return r.duration_seconds == null ? '—' : `${r.duration_seconds}s`
}

function durationType(r: EvalCaseResult) {
  if (r.duration_seconds == null) return 'info' as const
  return r.duration_seconds > 120 ? 'warning' as const : 'success' as const
}

const statusMeta = {
  running: { label: '执行中', type: 'primary' as const },
  completed: { label: '已完成', type: 'success' as const },
  failed: { label: '失败', type: 'danger' as const }
}

function statusOf(status: string) {
  return statusMeta[status as keyof typeof statusMeta] ?? { label: status, type: 'info' as const }
}
</script>

<template>
  <div class="evaluation-page">
    <div class="eval-header">
      <div>
        <h2>评测中心</h2>
        <p class="hint">
          选择数据集批量执行用例（standard = 10 例含 2 个提示注入防护用例 / smoke = 2 例快速冒烟），
          自动统计成功率、耗时、ReAct、关键词覆盖率、记忆命中率、Judge 质量分、防护通过率与成本
        </p>
      </div>
      <div class="run-controls">
        <el-select
          v-model="selectedDataset"
          size="large"
          :disabled="running"
          style="width: 220px"
        >
          <el-option
            v-for="d in datasets"
            :key="d.name"
            :label="`${d.name}（${d.case_count} 例）`"
            :value="d.name"
          />
        </el-select>
        <el-switch
          v-model="useJudge"
          size="large"
          :disabled="running"
          active-text="LLM Judge"
        />
        <el-button
          type="primary"
          size="large"
          :loading="starting"
          :disabled="running"
          @click="onRun"
        >
          {{ running ? '评测执行中…' : '运行评测' }}
        </el-button>
      </div>
    </div>

    <!-- 历史评测列表 -->
    <el-card shadow="never" class="eval-list-card">
      <template #header>
        <span class="card-title">历史评测</span>
        <el-tag v-if="running" type="primary" size="small" effect="plain">执行中</el-tag>
      </template>
      <el-table :data="reports" size="small" highlight-current-row @row-click="onRowClick">
        <el-table-column prop="eval_id" label="评测 ID" width="160">
          <template #default="{ row }">
            <code>{{ row.eval_id }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="开始时间" width="170" />
        <el-table-column label="数据集" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.dataset || 'standard' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusOf(row.status).type" size="small">{{ statusOf(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="140">
          <template #default="{ row }">
            <span>{{ row.completed_cases }} / {{ row.total_cases }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click="select(row.eval_id)">查看</el-button>
            <el-button
              link
              type="danger"
              :disabled="row.status === 'running'"
              @click="confirmDelete(row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!reports.length" description="暂无评测记录，点击「运行评测」开始" :image-size="70" />
    </el-card>

    <!-- 运行中进度 -->
    <el-card v-if="running && report" shadow="never" class="eval-progress-card">
      <template #header>
        <span class="card-title">评测执行中</span>
        <el-tag type="primary" size="small" effect="plain">每 2 秒自动刷新</el-tag>
      </template>
      <el-progress
        :percentage="Math.round((report.completed_cases / report.total_cases) * 100)"
        :status="'active'"
        :stroke-width="14"
        :text-inside="true"
      />
      <p class="hint progress-tip">
        已完成 {{ report.completed_cases }} / {{ report.total_cases }} 个用例
        （{{ report.eval_id }} · 数据集 {{ report.dataset }}）
      </p>
    </el-card>

    <el-empty v-if="!report && !running" description="选择左侧历史评测查看结果" />

    <!-- 评测结果 -->
    <template v-if="report && !running">
      <el-alert
        v-if="report.status === 'failed'"
        :title="`评测中断：${report.error || '未知错误'}`"
        type="error"
        show-icon
        :closable="false"
        class="eval-alert"
      />

      <!-- 回归门禁横幅（维度三）：pass 绿 / fail 红（列出阻断项）/ skipped 灰 -->
      <el-alert
        v-if="regressionInfo"
        :title="`${regressionInfo.title}（数据集 ${report.dataset}）`"
        :type="regressionInfo.type"
        show-icon
        :closable="false"
        class="eval-alert"
      >
        <div class="regression-body">
          <div>{{ regressionInfo.deltaText }}</div>
          <ul v-if="regressionInfo.blockers.length" class="regression-blockers">
            <li v-for="b in regressionInfo.blockers" :key="b">{{ b }}</li>
          </ul>
        </div>
      </el-alert>

      <!-- 指标卡（9 项） -->
      <el-row :gutter="16" class="metric-row">
        <el-col v-for="m in metricCards" :key="m.key" :span="8">
          <el-card shadow="hover" class="metric-card">
            <div class="metric-label">{{ m.label }}</div>
            <div class="metric-value" :class="{ 'metric-bad': m.met === false }">
              {{ m.value }}
            </div>
            <div class="metric-target">
              目标 {{ m.target }}
              <el-tag
                :type="m.met === null ? 'info' : (m.met ? 'success' : 'danger')"
                size="small"
                effect="plain"
              >
                {{ m.met === null ? '不适用' : (m.met ? '达标' : '未达标') }}
              </el-tag>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16">
        <!-- 雷达图 -->
        <el-col :span="10">
          <el-card shadow="never">
            <template #header>
              <span class="card-title">多维度指标雷达图</span>
              <el-tag size="small" type="info" effect="plain">归一化 0~100</el-tag>
            </template>
            <div ref="radarEl" class="radar-chart" />
          </el-card>
        </el-col>

        <!-- Markdown 报告 -->
        <el-col :span="14">
          <el-card shadow="never">
            <template #header>
              <div class="md-header">
                <span class="card-title">Markdown 评测报告</span>
                <el-switch v-model="showMd" size="small" active-text="显示" inactive-text="隐藏" />
              </div>
            </template>
            <div v-if="showMd" class="md-content" v-html="reportMdHtml" />
          </el-card>
        </el-col>
      </el-row>

      <!-- 用例明细 -->
      <el-card shadow="never" class="case-card">
        <template #header>
          <div class="md-header">
            <span class="card-title">用例明细（{{ summary?.success_cases }}/{{ summary?.total_cases }} 成功）</span>
            <el-switch v-model="showCases" size="small" active-text="显示" inactive-text="隐藏" />
          </div>
        </template>
        <el-table v-if="showCases" :data="report.results" size="small" border>
          <el-table-column prop="case_index" label="#" width="45" />
          <el-table-column prop="topic" label="主题" min-width="180" />
          <el-table-column label="类型" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.kind === 'guard'" :type="row.guard_pass ? 'success' : 'danger'" size="small">
                {{ row.guard_pass ? '🛡 防护通过' : '🛡 防护失败' }}
              </el-tag>
              <span v-else class="hint">标准</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="caseStatusType(row)" size="small">{{ caseStatusLabel(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="85">
            <template #default="{ row }">
              <el-tag :type="durationType(row)" size="small" effect="plain">{{ durationText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="react_loops" label="ReAct" width="70" />
          <el-table-column label="关键词覆盖率" width="110">
            <template #default="{ row }">
              {{ coveragePercent(row.keyword_coverage) }}
            </template>
          </el-table-column>
          <el-table-column prop="memory_hits" label="记忆命中" width="85" />
          <el-table-column label="质量分" width="90">
            <template #default="{ row }">
              <el-tag
                v-if="row.quality_score != null"
                :type="row.quality_score >= 3.5 ? 'success' : 'warning'"
                size="small"
                effect="plain"
              >{{ qualityText(row) }}</el-tag>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
          <el-table-column label="成本" width="95">
            <template #default="{ row }">
              <span>{{ costText(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="强制通过" width="85">
            <template #default="{ row }">
              <el-tag v-if="row.forced_pass" type="warning" size="small">⚠️ 是</el-tag>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="task_id" label="任务 ID" min-width="125">
            <template #default="{ row }">
              <code>{{ row.task_id }}</code>
            </template>
          </el-table-column>
          <el-table-column label="异常" min-width="130">
            <template #default="{ row }">
              <span v-if="row.error" class="error-text">{{ row.error }}</span>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.evaluation-page {
  max-width: 1200px;
  margin: 0 auto;
}
.eval-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.eval-header h2 {
  margin: 0 0 8px;
  font-size: 20px;
}
.run-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}
.regression-body {
  font-size: 13px;
  line-height: 20px;
}
.regression-blockers {
  margin: 6px 0 0;
  padding-left: 18px;
  color: #f56c6c;
}
.hint {
  color: #909399;
  font-size: 13px;
  line-height: 20px;
  margin: 0;
}
.card-title {
  font-weight: 600;
  font-size: 14px;
  margin-right: 8px;
}
.eval-list-card,
.eval-progress-card,
.eval-alert,
.metric-row,
.case-card {
  margin-bottom: 16px;
}
.progress-tip {
  margin-top: 10px;
}
.metric-card {
  text-align: center;
}
.metric-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 6px;
}
.metric-value {
  font-size: 24px;
  font-weight: 700;
  color: #303133;
}
.metric-bad {
  color: #f56c6c;
}
.metric-target {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}
.radar-chart {
  height: 320px;
}
.md-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.md-content {
  max-height: 320px;
  overflow: auto;
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  word-break: break-word;
}
.md-content h1 {
  font-size: 18px;
}
.md-content h2 {
  font-size: 16px;
}
.md-content table {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 12px;
}
.md-content th,
.md-content td {
  border: 1px solid #e4e7ed;
  padding: 5px 8px;
}
.error-text {
  color: #f56c6c;
  font-size: 12px;
}
</style>