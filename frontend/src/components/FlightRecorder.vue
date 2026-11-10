<script setup lang="ts">
/** Flight Recorder（维度一·可观测性）：任务执行全链路追踪。
 *
 * 三块内容：
 * 1. 时间轴瀑布图：node/llm/tool 三类 span 按开始时间排布，
 *    横轴为相对耗时（秒），条宽=span 时长，子 span 缩进展示父子层级；
 *    执行中通过 SSE 实时追加（span_start 画脉冲条，span_end 落定为实色条）。
 * 2. 成本卡：token 合计 / 成本合计（$ + ≈¥）/ 按模型分组 / 按工具分组（调用·重试·错误）。
 * 3. span 详情抽屉：点击任意条查看全部字段（tokens、成本、参数指纹、重试、错误类别）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  fetchTaskEvents,
  fetchTaskEventsSummary,
  streamTaskEvents,
  type EventSummary,
  type TaskEvent
} from '../api/tasks'

const props = defineProps<{
  taskId: string
  /** 任务状态：running 时开启 SSE 实时流 */
  status: string
}>()

const events = ref<TaskEvent[]>([])
/** 执行中尚未结束的 span（span_start 先到，span_end 到达后移入 events） */
const pending = ref<Map<string, TaskEvent>>(new Map())
const summary = ref<EventSummary | null>(null)
const selected = ref<TaskEvent | null>(null)
const drawerVisible = ref(false)
const loading = ref(false)

let eventSource: EventSource | null = null
let tickTimer: number | null = null
/** 强制刷新脉冲条动画（每 500ms 触发一次响应式重算） */
const now = ref(Date.now() / 1000)

// ---------------- 数据加载 ----------------

async function reload() {
  loading.value = true
  try {
    const [evts, sum] = await Promise.all([
      fetchTaskEvents(props.taskId),
      fetchTaskEventsSummary(props.taskId)
    ])
    events.value = evts
    summary.value = sum
  } catch {
    /* 任务无事件（旧任务）时静默 */
  } finally {
    loading.value = false
  }
}

function startStream() {
  stopStream()
  eventSource = streamTaskEvents(
    props.taskId,
    (evt) => {
      if (evt.kind === 'span_start' && evt.span) {
        pending.value.set(evt.span.span_id, evt.span)
      } else if (evt.kind === 'span_end' && evt.span) {
        pending.value.delete(evt.span.span_id)
        const idx = events.value.findIndex((e) => e.span_id === evt.span!.span_id)
        if (idx >= 0) events.value[idx] = evt.span
        else events.value.push(evt.span)
        void refreshSummary()
      } else if (evt.kind === 'done') {
        stopStream()
        void reload()
      }
    },
    () => {
      /* SSE 断开：依赖外层轮询兜底 */
    }
  )
  tickTimer = window.setInterval(() => {
    now.value = Date.now() / 1000
  }, 500)
}

async function refreshSummary() {
  try {
    summary.value = await fetchTaskEventsSummary(props.taskId)
  } catch {
    /* 忽略 */
  }
}

function stopStream() {
  eventSource?.close()
  eventSource = null
  if (tickTimer !== null) {
    window.clearInterval(tickTimer)
    tickTimer = null
  }
}

onMounted(async () => {
  await reload()
  if (props.status === 'running') startStream()
})

watch(
  () => props.status,
  (s) => {
    if (s === 'running') startStream()
    else {
      stopStream()
      void reload()
    }
  }
)

onBeforeUnmount(stopStream)

// ---------------- 瀑布图布局 ----------------

interface Row {
  span: TaskEvent
  depth: number
  isPending: boolean
}

/** 全部待渲染 span（已完成 + 执行中），按开始时间排序 */
const allSpans = computed<TaskEvent[]>(() => {
  const list = [...events.value, ...pending.value.values()]
  return list.sort((a, b) => a.start_ts - b.start_ts)
})

const t0 = computed(() => (allSpans.value.length ? Math.min(...allSpans.value.map((s) => s.start_ts)) : 0))

/** 时间轴总长（秒）：执行中用当前时间作为右边界，保证脉冲条可见 */
const totalSeconds = computed(() => {
  if (!allSpans.value.length) return 1
  const ends = allSpans.value.map((s) => (s.end_ts || now.value))
  return Math.max(0.1, Math.max(...ends) - t0.value)
})

/** 树形层级：node 为 depth0，其子 span 紧随其后 depth1 */
const rows = computed<Row[]>(() => {
  const pendingIds = new Set(pending.value.keys())
  return allSpans.value.map((span) => ({
    span,
    depth: span.parent_span_id ? 1 : 0,
    isPending: pendingIds.has(span.span_id)
  }))
})

function barStyle(row: Row) {
  const start = ((row.span.start_ts - t0.value) / totalSeconds.value) * 100
  const end = row.isPending ? now.value : row.span.end_ts
  const width = Math.max(0.8, ((end - row.span.start_ts) / totalSeconds.value) * 100)
  return { left: `${start}%`, width: `${Math.min(width, 100 - start)}%` }
}

function barClass(row: Row) {
  if (row.span.status === 'error') return 'bar bar-error'
  return `bar bar-${row.span.span_type}${row.isPending ? ' bar-pending' : ''}`
}

/** 时间轴刻度（5 格，秒） */
const ticks = computed(() => {
  const n = 4
  return Array.from({ length: n + 1 }, (_, i) => ({
    left: `${(i / n) * 100}%`,
    label: `${((totalSeconds.value * i) / n).toFixed(1)}s`
  }))
})

// ---------------- 格式化 ----------------

function fmtDuration(ms: number | null | undefined) {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

const CNY_RATE = 7.2 // 固定演示汇率（成本卡 ≈¥ 换算）

function fmtCost(usd: number | null | undefined) {
  if (!usd) return '$0.00'
  if (usd < 0.01) return `$${usd.toFixed(6)}`
  return `$${usd.toFixed(4)}`
}

function fmtCostCny(usd: number | null | undefined) {
  if (!usd) return '¥0.00'
  return `≈¥${(usd * CNY_RATE).toFixed(4)}`
}

function spanLabel(row: Row) {
  const s = row.span
  if (s.span_type === 'node') return `节点 · ${s.name}`
  if (s.span_type === 'llm') return `LLM · ${s.model || s.name}`
  return `工具 · ${s.tool_name || s.name}`
}

const typeMeta: Record<string, { label: string; type: 'primary' | 'warning' | 'success' }> = {
  node: { label: '图节点', type: 'primary' },
  llm: { label: 'LLM 调用', type: 'warning' },
  tool: { label: '工具调用', type: 'success' }
}

function openDetail(row: Row) {
  selected.value = row.span
  drawerVisible.value = true
}
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="flight-card">
    <template #header>
      <div class="flight-header">
        <span class="card-title">Flight Recorder · 运行追踪</span>
        <div class="flight-legend">
          <span class="legend-item"><i class="dot dot-node" />图节点</span>
          <span class="legend-item"><i class="dot dot-llm" />LLM</span>
          <span class="legend-item"><i class="dot dot-tool" />工具</span>
          <span class="legend-item"><i class="dot dot-error" />失败</span>
          <el-tag v-if="status === 'running'" type="primary" size="small" effect="plain">
            SSE 实时追踪中…
          </el-tag>
        </div>
      </div>
    </template>

    <el-empty
      v-if="!allSpans.length && !loading"
      description="暂无可观测数据（该任务可能早于可观测性版本执行）"
      :image-size="60"
    />

    <div v-else class="flight-body">
      <!-- 左：时间轴瀑布图 -->
      <div class="waterfall">
        <div class="axis">
          <span v-for="t in ticks" :key="t.left" class="tick" :style="{ left: t.left }">{{ t.label }}</span>
        </div>
        <div
          v-for="row in rows"
          :key="row.span.span_id"
          class="wf-row"
          :class="{ 'wf-row-child': row.depth > 0 }"
          @click="openDetail(row)"
        >
          <div class="wf-label" :class="{ 'wf-label-child': row.depth > 0 }">
            <el-tag :type="typeMeta[row.span.span_type]?.type" size="small" effect="plain" class="wf-type">
              {{ typeMeta[row.span.span_type]?.label }}
            </el-tag>
            <span class="wf-name">{{ spanLabel(row) }}</span>
          </div>
          <div class="wf-track">
            <div :class="barClass(row)" :style="barStyle(row)">
              <span class="bar-text">{{ row.isPending ? '…' : fmtDuration(row.span.duration_ms) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 右：成本卡 -->
      <div v-if="summary" class="cost-panel">
        <div class="cost-grid">
          <div class="cost-item">
            <div class="cost-num">{{ fmtCost(summary.cost_usd) }}</div>
            <div class="cost-label">LLM 成本（{{ fmtCostCny(summary.cost_usd) }}）</div>
          </div>
          <div class="cost-item">
            <div class="cost-num">{{ summary.tokens_in + summary.tokens_out }}</div>
            <div class="cost-label">Token（入 {{ summary.tokens_in }} / 出 {{ summary.tokens_out }}）</div>
          </div>
          <div class="cost-item">
            <div class="cost-num">{{ summary.llm_calls }}</div>
            <div class="cost-label">LLM 调用</div>
          </div>
          <div class="cost-item">
            <div class="cost-num">{{ summary.tool_calls }}</div>
            <div class="cost-label">工具调用</div>
          </div>
          <div class="cost-item">
            <div class="cost-num" :class="{ 'cost-danger': summary.error_count > 0 }">
              {{ summary.error_count }}
            </div>
            <div class="cost-label">失败 span</div>
          </div>
          <div class="cost-item">
            <div class="cost-num">{{ fmtDuration(summary.total_duration_ms) }}</div>
            <div class="cost-label">墙钟耗时</div>
          </div>
        </div>

        <div v-if="Object.keys(summary.models).length" class="cost-section">
          <div class="cost-section-title">按模型</div>
          <div v-for="(m, name) in summary.models" :key="name" class="cost-line">
            <code class="cost-line-name">{{ name }}</code>
            <span class="cost-line-val">
              {{ m.calls }} 次 · {{ m.tokens_in + m.tokens_out }} tok · {{ fmtCost(m.cost_usd) }}
            </span>
          </div>
        </div>

        <div v-if="Object.keys(summary.tools).length" class="cost-section">
          <div class="cost-section-title">按工具</div>
          <div v-for="(t, name) in summary.tools" :key="name" class="cost-line">
            <code class="cost-line-name">{{ name }}</code>
            <span class="cost-line-val">
              {{ t.calls }} 次
              <template v-if="t.retries"> · 重试 {{ t.retries }}</template>
              <template v-if="t.errors"> · <span class="cost-danger">失败 {{ t.errors }}</span></template>
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- span 详情抽屉 -->
    <el-drawer v-model="drawerVisible" title="Span 详情" size="420px">
      <template v-if="selected">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="类型">
            <el-tag :type="typeMeta[selected.span_type]?.type" size="small">
              {{ typeMeta[selected.span_type]?.label }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="名称">{{ selected.name }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="selected.status === 'error' ? 'danger' : 'success'" size="small">
              {{ selected.status === 'error' ? '失败' : '成功' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="耗时">{{ fmtDuration(selected.duration_ms) }}</el-descriptions-item>
          <el-descriptions-item label="span_id"><code>{{ selected.span_id }}</code></el-descriptions-item>
          <el-descriptions-item v-if="selected.parent_span_id" label="父 span">
            <code>{{ selected.parent_span_id }}</code>
          </el-descriptions-item>
          <template v-if="selected.span_type === 'llm'">
            <el-descriptions-item label="模型"><code>{{ selected.model }}</code></el-descriptions-item>
            <el-descriptions-item label="Token 入/出">
              {{ selected.tokens_in ?? '—' }} / {{ selected.tokens_out ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="成本">
              {{ fmtCost(selected.cost_usd) }}（{{ fmtCostCny(selected.cost_usd) }}）
            </el-descriptions-item>
          </template>
          <template v-if="selected.span_type === 'tool'">
            <el-descriptions-item label="工具"><code>{{ selected.tool_name }}</code></el-descriptions-item>
            <el-descriptions-item label="参数指纹"><code>{{ selected.args_hash }}</code></el-descriptions-item>
            <el-descriptions-item label="重试次数">{{ selected.retry_count ?? 0 }}</el-descriptions-item>
          </template>
          <el-descriptions-item v-if="selected.error_class" label="错误类别">
            <el-tag type="danger" size="small">{{ selected.error_class }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="开始时间">
            {{ new Date(selected.start_ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) }}
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="Object.keys(selected.detail || {}).length" class="detail-json">
          <div class="cost-section-title">附加信息</div>
          <pre>{{ JSON.stringify(selected.detail, null, 2) }}</pre>
        </div>
      </template>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.flight-card {
  margin-bottom: 16px;
}
.flight-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.flight-legend {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: #909399;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  display: inline-block;
}
.dot-node { background: #409eff; }
.dot-llm { background: #e6a23c; }
.dot-tool { background: #67c23a; }
.dot-error { background: #f56c6c; }

.flight-body {
  display: flex;
  gap: 20px;
}
/* 瀑布图 */
.waterfall {
  flex: 1;
  min-width: 0;
}
.axis {
  position: relative;
  height: 18px;
  margin-left: 220px;
  border-bottom: 1px solid #e4e7ed;
}
.tick {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  font-size: 11px;
  color: #c0c4cc;
}
.wf-row {
  display: flex;
  align-items: center;
  height: 30px;
  cursor: pointer;
  border-radius: 4px;
}
.wf-row:hover {
  background: #f5f7fa;
}
.wf-label {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
}
.wf-label-child {
  padding-left: 20px;
}
.wf-type {
  flex-shrink: 0;
}
.wf-name {
  font-size: 12px;
  color: #606266;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wf-track {
  position: relative;
  flex: 1;
  height: 18px;
}
.bar {
  position: absolute;
  top: 0;
  height: 18px;
  border-radius: 3px;
  min-width: 4px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.bar-text {
  font-size: 10px;
  color: rgba(255, 255, 255, 0.95);
  padding-right: 4px;
  white-space: nowrap;
}
.bar-node { background: #409eff; }
.bar-llm { background: #e6a23c; }
.bar-tool { background: #67c23a; }
.bar-error { background: #f56c6c; }
.bar-pending {
  opacity: 0.7;
  animation: pulse 1s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 0.9; }
}

/* 成本卡 */
.cost-panel {
  width: 300px;
  flex-shrink: 0;
  border-left: 1px dashed #e4e7ed;
  padding-left: 20px;
}
.cost-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.cost-item {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 10px;
}
.cost-num {
  font-size: 18px;
  font-weight: 700;
  color: #303133;
}
.cost-label {
  font-size: 11px;
  color: #909399;
  margin-top: 2px;
}
.cost-danger {
  color: #f56c6c;
}
.cost-section {
  margin-top: 14px;
}
.cost-section-title {
  font-size: 12px;
  font-weight: 600;
  color: #909399;
  margin-bottom: 6px;
}
.cost-line {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed #f0f0f0;
}
.cost-line-name {
  color: #606266;
}
.cost-line-val {
  color: #909399;
  text-align: right;
}
.detail-json {
  margin-top: 12px;
}
.detail-json pre {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  overflow: auto;
}

@media (max-width: 1100px) {
  .flight-body {
    flex-direction: column;
  }
  .cost-panel {
    width: 100%;
    border-left: none;
    padding-left: 0;
  }
  .axis {
    margin-left: 160px;
  }
  .wf-label {
    width: 160px;
  }
}
</style>
