import axios from 'axios'

/** 工具调用记录 */
export interface ToolCallRecord {
  name: string
  arguments: Record<string, unknown>
  result: unknown
  success: boolean
}

/** 单智能体对话响应 */
export interface ChatResult {
  reply: string
  tool_calls: ToolCallRecord[]
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 180000 // 对话可能触发多轮联网搜索+抓取（单轮搜索约 10s），放宽到 3 分钟
})

/** 发送消息给单智能体，触发工具调用（提示词触发） */
export async function sendChat(message: string, history: unknown[] = []): Promise<ChatResult> {
  const { data } = await http.post('/chat', { message, history })
  return data as ChatResult
}
