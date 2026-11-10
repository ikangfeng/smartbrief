<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { submitTask } from '../api/tasks'

const router = useRouter()
const topic = ref('')
const submitting = ref(false)

// 维度二：人工审批开关（开启 = manual 双闸门；关闭 = auto 闸门直通）+ 预算护栏
const approvalEnabled = ref(true)
const maxTokens = ref<number | null>(null)
const maxDurationS = ref<number | null>(null)

async function onSubmit() {
  const value = topic.value.trim()
  if (!value) {
    ElMessage.warning('请输入研究主题')
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    const result = await submitTask(value, {
      approval_mode: approvalEnabled.value ? 'manual' : 'auto',
      max_tokens: maxTokens.value || null,
      max_duration_s: maxDurationS.value || null
    })
    ElMessage.success(`任务已提交：${result.task_id}`)
    router.push(`/tasks/${result.task_id}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '提交失败，请检查后端服务')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="home-page">
    <div class="hero">
      <h2>多智能体报告生成</h2>
      <p class="hint">
        输入研究主题，系统将自动规划执行步骤（Plan-and-Execute），
        由 Researcher 联网搜索数据、Writer 撰写报告草稿，全程可视化展示 ReAct 推理过程
      </p>
    </div>

    <el-card shadow="never" class="submit-card">
      <el-input
        v-model="topic"
        type="textarea"
        :rows="3"
        maxlength="200"
        show-word-limit
        placeholder="例如：智能手表 2025 市场分析、便携式储能电源 北美市场 2026…"
        @keydown.enter.exact.prevent="onSubmit"
      />
      <!-- 维度二：审批模式 + 预算护栏（功能控制） -->
      <div class="control-row">
        <span class="control-label">人工审批</span>
        <el-switch
          v-model="approvalEnabled"
          inline-prompt
          active-text="开"
          inactive-text="关"
        />
        <span class="control-tip">{{ approvalEnabled ? '计划/报告两道闸门，批准后继续' : '闸门直通，一把跑完' }}</span>
        <span class="control-label budget-label">预算护栏</span>
        <el-input-number
          v-model="maxTokens"
          :min="0"
          :step="1000"
          size="small"
          placeholder="token 上限"
          controls-position="right"
          class="budget-input"
        />
        <el-input-number
          v-model="maxDurationS"
          :min="0"
          :step="30"
          size="small"
          placeholder="时长上限(秒)"
          controls-position="right"
          class="budget-input"
        />
      </div>
      <p class="control-hint">
        人工审批开启时：计划生成后、报告输出前各挂起一次，可批准 / 改后批准 / 退回；关闭则全流程自动执行。
        预算护栏：token / 时长超限即强制收口（留空不限）。
      </p>
      <div class="submit-actions">
        <el-button type="primary" size="large" :loading="submitting" @click="onSubmit">
          生成研究报告
        </el-button>
        <el-button size="large" @click="router.push('/tasks')">历史任务</el-button>
      </div>
    </el-card>

    <el-row :gutter="16" class="feature-row">
      <el-col :span="8">
        <el-card shadow="hover" class="feature-card">
          <h3>Plan 规划</h3>
          <p class="hint">Planner Agent 将主题拆解为多步执行计划（含依赖关系）</p>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover" class="feature-card">
          <h3>ReAct 搜索</h3>
          <p class="hint">Researcher 通过 Thought→Action→Observation 循环联网搜索数据</p>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover" class="feature-card">
          <h3>Writer 撰写</h3>
          <p class="hint">Writer 基于研究数据生成结构化报告草稿</p>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.home-page {
  max-width: 960px;
  margin: 0 auto;
}
.hero {
  text-align: center;
  margin-bottom: 24px;
}
.hero h2 {
  margin: 0 0 8px;
  font-size: 24px;
}
.hint {
  color: #909399;
  font-size: 13px;
  line-height: 20px;
}
.submit-card {
  margin-bottom: 24px;
}
.submit-actions {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
/* 维度二：审批模式 + 预算护栏 */
.control-row {
  margin-top: 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.control-label {
  font-size: 13px;
  color: #606266;
}
.control-tip {
  font-size: 12px;
  color: #909399;
}
.budget-label {
  margin-left: 12px;
}
.budget-input {
  width: 150px;
}
.control-hint {
  margin: 8px 0 0;
  font-size: 12px;
  color: #909399;
}
.feature-row .el-col {
  display: flex;
}
.feature-card {
  flex: 1;
}
.feature-row h3 {
  margin: 0 0 8px;
  font-size: 15px;
}
</style>
