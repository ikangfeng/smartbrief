/** 报告 Markdown 渲染（报告质量增强）：
 * - ```chart 围栏块 → ECharts 真图表（JSON 解析失败时保留原代码块降级展示）
 * - 行内引用 [n] → 可点击锚点链接（跳转到文末「参考资料」对应条目）
 * 输出经 DOMPurify 过滤，防 XSS。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import * as echarts from 'echarts'

export interface ChartBlock {
  option: Record<string, any>
  raw: string
}

const CHART_FENCE_RE = /```chart\s*\n([\s\S]*?)```/g
const FENCE_RE = /(```[\s\S]*?```)/g
const INLINE_CODE_RE = /(`[^`\n]*`)/g
// [n]（1-2 位数字），排除 Markdown 链接语法 [n]( 与脚注定义 [n]:
const CITE_RE = /\[(\d{1,2})\](?!\(|\]:)/g

/** [n] → 引用锚点链接（跳过行内代码段，避免误伤数组字面量如 [1, 2]） */
function linkifyCitations(segment: string): string {
  return segment
    .split(INLINE_CODE_RE)
    .map((part) =>
      part.startsWith('`')
        ? part
        : part.replace(CITE_RE, '<a class="cite" href="#ref-$1">[$1]</a>')
    )
    .join('')
}

/** Markdown → 安全 HTML + 提取的图表配置列表 */
export function renderReport(md: string): { html: string; charts: ChartBlock[] } {
  const charts: ChartBlock[] = []
  if (!md) return { html: '', charts }
  // 1) ```chart 块 → 占位 div（渲染阶段挂载 ECharts；解析失败保留原代码块）
  let pre = md.replace(CHART_FENCE_RE, (match, body: string) => {
    try {
      const option = JSON.parse(body.trim()) as Record<string, any>
      charts.push({ option, raw: body })
      return `\n\n<div class="chart-placeholder" data-chart-idx="${charts.length - 1}"></div>\n\n`
    } catch {
      return match
    }
  })
  // 2) 行内引用 [n] → 锚点链接（跳过剩余围栏代码块）
  pre = pre
    .split(FENCE_RE)
    .map((part) => (part.startsWith('```') ? part : linkifyCitations(part)))
    .join('')
  const raw = marked.parse(pre, { async: false }) as string
  const html = DOMPurify.sanitize(raw, { ADD_ATTR: ['data-chart-idx'] })
  return { html, charts }
}

/** LLM 生成的 chart option 只保证数据正确，布局常撞车（长标题压住顶部图例）。
 * 挂载前统一兜底：标题置顶居中、图例置底、grid 让出上下空间；
 * LLM 显式给出的同名布局字段优先（spreads 在后）。 */
export function normalizeChartOption(option: Record<string, any>): Record<string, any> {
  const opt = { ...option }
  const hasTitle = !!opt.title && !Array.isArray(opt.title)
  const hasLegend = !!opt.legend
  if (hasTitle) {
    opt.title = { left: 'center', top: 4, textStyle: { fontSize: 14 }, ...opt.title }
  }
  if (hasLegend) {
    const legend = (Array.isArray(opt.legend) ? opt.legend[0] : opt.legend) ?? {}
    const { top: _dropTop, ...rest } = legend   // 顶部图例是撞车元凶，统一改置底
    opt.legend = { bottom: 0, ...rest }
  }
  if (hasTitle || hasLegend) {
    const grid = (Array.isArray(opt.grid) ? opt.grid[0] : opt.grid) ?? {}
    opt.grid = {
      top: hasTitle ? 56 : 32,
      bottom: hasLegend ? 44 : 24,
      containLabel: true,
      ...grid,
    }
  }
  return opt
}

/** 把报告容器内的占位 div 挂载为 ECharts 实例（失败保持空占位，不影响正文） */
export function mountReportCharts(container: HTMLElement, charts: ChartBlock[]): void {
  charts.forEach((chart, idx) => {
    const el = container.querySelector<HTMLElement>(`.chart-placeholder[data-chart-idx="${idx}"]`)
    if (!el) return
    try {
      echarts.init(el).setOption(normalizeChartOption(chart.option))
    } catch {
      /* 图表配置非法时保持占位，不阻塞报告展示 */
    }
  })
}
