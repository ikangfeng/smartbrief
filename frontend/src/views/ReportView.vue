<script setup lang="ts">
/** 报告展示页（第 27 节最终完善）：Markdown 渲染 + 图表图片 + 执行摘要 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import html2pdf from 'html2pdf.js'
import * as echarts from 'echarts'
import { fetchTaskReport, scoreTaskReport, type ReportScore, type TaskReport } from '../api/tasks'
import { distillSkill } from '../api/skills'
import { mountReportCharts, renderReport } from '../utils/reportMarkdown'

const route = useRoute()
const router = useRouter()
const taskId = route.params.taskId as string

const report = ref<TaskReport | null>(null)
const loading = ref(false)
const notFound = ref(false)
const reportEl = ref<HTMLElement>()

async function load() {
  loading.value = true
  try {
    report.value = await fetchTaskReport(taskId)
    notFound.value = false
  } catch (e: any) {
    if (e?.response?.status === 404) {
      notFound.value = true
    } else {
      ElMessage.error('获取报告失败，请检查后端服务')
    }
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** Markdown → 安全 HTML（```chart → ECharts 占位；[n] → 引用锚点链接） */
const rendered = computed(() => renderReport(report.value?.content_markdown ?? ''))
const reportHtml = computed(() => rendered.value.html)

/** HTML 渲染后把图表占位 div 挂载为 ECharts 实例 */
watch(rendered, async () => {
  await nextTick()
  if (reportEl.value) mountReportCharts(reportEl.value, rendered.value.charts)
})

/** 执行摘要卡（耗时可能为空 → 显示 —） */
const summaryItems = computed(() => {
  const s = report.value?.executive_summary
  if (!s) return []
  return [
    { label: '执行总耗时', value: s.total_time == null ? '—' : `${s.total_time}s` },
    { label: 'ReAct 行动次数', value: `${s.react_loops} 次` },
    { label: '长期记忆命中', value: `${s.memory_hit_count} 条` }
  ]
})

function formatTime(seconds: number | null) {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds} 秒`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m} 分 ${s} 秒`
}

/** 导出 PDF：基于渲染后的报告 DOM 生成 */
async function exportPdf() {
  if (!reportEl.value) return
  try {
    await html2pdf()
      .set({
        margin: 10,
        filename: `${report.value?.title ?? '报告'}.pdf`,
        image: { type: 'jpeg', quality: 0.95 },
        html2canvas: { scale: 2, useCORS: true },
        jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' }
      })
      .from(reportEl.value)
      .save()
    ElMessage.success('PDF 已导出')
  } catch {
    ElMessage.error('PDF 导出失败')
  }
}

/** 导出 Word：生成 .doc（HTML 兼容格式） */
function exportWord() {
  const html = `<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">
<head><meta charset="utf-8"><title>${report.value?.title ?? '报告'}</title></head>
<body>${reportHtml.value}</body></html>`
  const blob = new Blob(['\ufeff', html], { type: 'application/msword' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${report.value?.title ?? '报告'}.doc`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('Word 文档已导出')
}

/** 报告 → Skill 闭环：把本报告的写作方法沉淀为可复用技能（下次撰写自动挂载） */
const distilling = ref(false)
async function distill() {
  distilling.value = true
  try {
    const skill = await distillSkill(taskId)
    ElMessage.success(`已沉淀为技能「${skill.display_name}」，下次撰写同类报告将自动挂载`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '技能沉淀失败')
  } finally {
    distilling.value = false
  }
}

/** 单篇报告质量评分（五维，评审模型独立于工作者模型） */
const scoring = ref(false)
const score = ref<ReportScore | null>(null)
const scoreRadarEl = ref<HTMLElement>()
let scoreRadar: echarts.ECharts | null = null

/** 五维规格：固定顺序展示，键名做容错映射（Judge 可能返回中英文或缺维度） */
const DIM_SPECS: { label: string; aliases: string[] }[] = [
  { label: '相关性', aliases: ['relevance', '相关性'] },
  { label: '结构', aliases: ['structure', '结构'] },
  { label: '证据', aliases: ['evidence', '证据'] },
  { label: '洞察', aliases: ['insight', '洞察'] },
  { label: '表达', aliases: ['expression', '表达'] }
]
function dimValue(dims: Record<string, number>, aliases: string[]): number | null {
  for (const a of aliases) {
    const v = dims?.[a]
    if (typeof v === 'number') return v
  }
  return null
}
const scoreRadarOption = computed(() => {
  if (!score.value) return null
  const dims = score.value.dimensions ?? {}
  const values = DIM_SPECS.map((s) => dimValue(dims, s.aliases) ?? 0)
  return {
    radar: {
      indicator: DIM_SPECS.map((s) => ({ name: s.label, max: 5 })),
      radius: '72%',
      center: ['50%', '52%'],
      // 维度名直接带分数（如「结构 3.5」），信息密度与评测中心一致
      axisName: {
        color: '#606266',
        fontSize: 13,
        formatter: (name: string) => {
          const idx = DIM_SPECS.findIndex((s) => s.label === name)
          return `${name}  ${values[idx] ?? 0}`
        }
      },
      splitArea: {
        areaStyle: { color: ['#fff', '#f7f9fc'] }
      }
    },
    series: [
      {
        type: 'radar',
        data: [{ value: values, name: '五维评分' }],
        symbolSize: 5,
        lineStyle: { width: 2, color: '#409eff' },
        itemStyle: { color: '#409eff' },
        areaStyle: { opacity: 0.25, color: '#409eff' }
      }
    ]
  }
})
async function runScore() {
  scoring.value = true
  try {
    score.value = await scoreTaskReport(taskId)
    await nextTick()
    if (scoreRadarEl.value) {
      scoreRadar ??= echarts.init(scoreRadarEl.value)
      scoreRadar.setOption(scoreRadarOption.value!)
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '评分失败')
  } finally {
    scoring.value = false
  }
}
</script>

<template>
  <div v-loading="loading" class="report-page">
    <div class="report-header">
      <div>
        <el-button link @click="router.push('/reports')">← 返回报告中心</el-button>
        <h2 class="report-title">{{ report?.title || '报告展示' }}</h2>
        <p class="hint"><code>{{ taskId }}</code></p>
      </div>
      <div v-if="report" class="report-actions">
        <el-button size="small" :loading="scoring" @click="runScore">评估本报告</el-button>
        <el-button size="small" :loading="distilling" @click="distill">沉淀为 Skill</el-button>
        <el-button size="small" @click="exportWord">导出 Word</el-button>
        <el-button size="small" type="primary" @click="exportPdf">导出 PDF</el-button>
      </div>
    </div>

    <el-empty v-if="notFound" description="报告不存在或任务尚未完成" />

    <template v-if="report">
      <!-- 执行摘要卡 -->
      <el-row :gutter="16" class="summary-row">
        <el-col v-for="item in summaryItems" :key="item.label" :span="8">
          <el-card shadow="hover" class="summary-card">
            <div class="summary-label">{{ item.label }}</div>
            <div class="summary-value">{{ item.value }}</div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 单篇质量评分卡（评估本报告按钮触发；样式与评测中心雷达图一致） -->
      <el-card v-if="score" shadow="never" class="score-card">
        <template #header>
          <span class="card-title">质量评分（评审模型：{{ score.judge_model }}）</span>
        </template>
        <div class="score-body">
          <div class="score-left">
            <div class="score-total">{{ score.score ?? '—' }}<span class="score-max">/5</span></div>
            <div class="score-dim-list">
              <div v-for="s in DIM_SPECS" :key="s.label" class="score-dim-row">
                <span class="dim-label">{{ s.label }}</span>
                <el-progress
                  :percentage="((dimValue(score.dimensions ?? {}, s.aliases) ?? 0) / 5) * 100"
                  :stroke-width="8"
                  :format="() => `${dimValue(score!.dimensions ?? {}, s.aliases) ?? 0}`"
                />
              </div>
            </div>
          </div>
          <div ref="scoreRadarEl" class="score-radar" />
        </div>
        <p class="score-rationale">评语：{{ score.rationale }}</p>
      </el-card>

      <!-- 图表图片（Analyst 图表 Agent 属后续扩展，当前为空） -->
      <el-card v-if="report.charts.length" shadow="never" class="chart-card">
        <template #header>
          <span class="card-title">图表</span>
        </template>
        <div v-for="chart in report.charts" :key="chart.title" class="chart-item">
          <h3>{{ chart.title }}</h3>
          <img :src="chart.image_base64" alt="" />
        </div>
      </el-card>

      <!-- Markdown 报告 -->
      <el-card shadow="never" class="report-card">
        <template #header>
          <span class="card-title">调研简报</span>
          <el-tag size="small" type="warning" effect="plain">
            执行耗时 {{ formatTime(report.executive_summary.total_time) }}
          </el-tag>
        </template>
        <div ref="reportEl" class="report-content" v-html="reportHtml" />
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.report-page {
  max-width: 960px;
  margin: 0 auto;
}
.report-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.report-title {
  margin: 12px 0 8px;
  font-size: 20px;
}
.hint {
  color: #909399;
  font-size: 13px;
}
.report-actions {
  display: flex;
  gap: 8px;
}
.summary-row {
  margin-bottom: 16px;
}
.summary-card {
  text-align: center;
}
.summary-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 6px;
}
.summary-value {
  font-size: 20px;
  font-weight: 700;
  color: #303133;
}
.score-card {
  margin-bottom: 16px;
}
.score-body {
  display: flex;
  align-items: center;
  gap: 32px;
}
.score-total {
  font-size: 36px;
  font-weight: 700;
  color: #409eff;
}
.score-max {
  font-size: 16px;
  color: #909399;
}
.score-left {
  width: 260px;
  flex-shrink: 0;
}
.score-dim-list {
  margin-top: 12px;
}
.score-dim-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.score-dim-row .dim-label {
  width: 52px;
  font-size: 13px;
  color: #606266;
  flex-shrink: 0;
}
.score-dim-row :deep(.el-progress) {
  flex: 1;
}
.score-radar {
  flex: 1;
  height: 320px;
}
.score-rationale {
  margin-top: 12px;
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}
.chart-card {
  margin-bottom: 16px;
}
.chart-item {
  margin-bottom: 12px;
}
.chart-item img {
  max-width: 100%;
}
.card-title {
  font-weight: 600;
  font-size: 14px;
  margin-right: 8px;
}
.report-card {
  margin-bottom: 24px;
}
.report-content {
  margin: 0;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
}
/* Markdown 渲染后的富文本排版（与任务详情页一致） */
.report-content h1,
.report-content h2,
.report-content h3 {
  margin: 1em 0 0.5em;
}
.report-content h1 {
  font-size: 22px;
}
.report-content h2 {
  font-size: 18px;
}
.report-content h3 {
  font-size: 16px;
}
.report-content p {
  margin: 0.5em 0;
}
.report-content ul,
.report-content ol {
  padding-left: 1.6em;
  margin: 0.5em 0;
}
.report-content blockquote {
  margin: 0.5em 0;
  padding-left: 12px;
  border-left: 3px solid #e4e7ed;
  color: #606266;
}
.report-content code {
  background: #f5f7fa;
  border-radius: 3px;
  padding: 1px 5px;
  font-size: 13px;
}
.report-content pre {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 12px;
  overflow: auto;
}
.report-content table {
  border-collapse: collapse;
  margin: 0.5em 0;
}
.report-content th,
.report-content td {
  border: 1px solid #e4e7ed;
  padding: 6px 10px;
}
.report-content a {
  color: #409eff;
}
/* 报告质量增强：```chart 图表占位（渲染后由 ECharts 接管）与 [n] 引用链接 */
.report-content :deep(.chart-placeholder) {
  height: 320px;
  margin: 12px 0;
}
.report-content :deep(.cite) {
  color: #409eff;
  text-decoration: none;
  font-size: 12px;
  vertical-align: super;
  padding: 0 1px;
}
</style>