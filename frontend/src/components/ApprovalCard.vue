<script setup lang="ts">
/**
 * 人工审批卡片（维度二）：计划闸门（gate=plan）/ 报告闸门（gate=report）。
 *
 * - 展示待审内容快照（计划步骤列表 / 报告 Markdown 预览）与 payload_hash 指纹
 * - 三个决策动作：批准 / 改后批准（进入编辑态后提交）/ 退回（需填意见）
 * - version 乐观锁：提交带读取时 version，409 时提示刷新
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { decideApproval, type Approval } from '../api/tasks'

const props = defineProps<{ taskId: string; approval: Approval }>()
const emit = defineEmits<{ decided: [] }>()

const approver = ref('售前演示')
const comment = ref('')
const editing = ref(false)
const editText = ref('')
const submitting = ref(false)

const isPlan = computed(() => props.approval.gate === 'plan')

const planSteps = computed(() =>
  isPlan.value && Array.isArray(props.approval.payload_snapshot)
    ? (props.approval.payload_snapshot as Array<{ id: number; agent: string; task: string; depends_on: number[] }>)
    : []
)

const reportHtml = computed(() => {
  if (isPlan.value) return ''
  const md = String(props.approval.payload_snapshot ?? '')
  return DOMPurify.sanitize(marked.parse(md, { async: false }) as string)
})

// 切换审批记录时重置编辑态（同一任务先后两道闸门复用本组件）
watch(
  () => props.approval.id,
  () => {
    editing.value = false
    comment.value = ''
  }
)

function startEdit() {
  editText.value = isPlan.value
    ? JSON.stringify(props.approval.payload_snapshot, null, 2)
    : String(props.approval.payload_snapshot ?? '')
  editing.value = true
}

async function submit(action: 'approve' | 'edit' | 'reject') {
  if (submitting.value) return
  let editedPayload: unknown
  if (action === 'edit') {
    const text = editText.value.trim()
    if (!text) {
      ElMessage.warning('请先修改内容再提交「改后批准」')
      return
    }
    if (isPlan.value) {
      try {
        const parsed = JSON.parse(text)
        if (!Array.isArray(parsed) || parsed.length === 0) throw new Error('计划需为非空 JSON 数组')
        editedPayload = parsed
      } catch (e: any) {
        ElMessage.error(`计划 JSON 非法：${e.message}`)
        return
      }
    } else {
      editedPayload = text
    }
  }
  if (action === 'reject' && !comment.value.trim()) {
    ElMessage.warning('退回时请填写审批意见（说明退回原因）')
    return
  }
  if (action === 'reject') {
    try {
      await ElMessageBox.confirm(
        '退回后任务将以「审批退回」终态结束，确认退回吗？',
        '退回确认',
        { type: 'warning', confirmButtonText: '确认退回', cancelButtonText: '再想想' }
      )
    } catch {
      return
    }
  }
  submitting.value = true
  try {
    await decideApproval(props.taskId, {
      action,
      version: props.approval.version,
      approver: approver.value || '人工',
      comment: comment.value,
      edited_payload: editedPayload
    })
    ElMessage.success({ approve: '已批准，任务继续执行', edit: '已按修订批准，任务继续执行', reject: '已退回，任务终止' }[action])
    emit('decided')
  } catch (e: any) {
    const status = e?.response?.status
    if (status === 409) {
      ElMessage.error('审批记录已被他人更新，请刷新后重试')
      emit('decided')
    } else {
      ElMessage.error(e?.response?.data?.detail || '提交审批失败')
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-card shadow="never" class="approval-card">
    <template #header>
      <div class="approval-header">
        <span class="card-title">{{ isPlan ? '计划审批（闸门 1/2）' : '报告终审（闸门 2/2）' }}</span>
        <div class="approval-tags">
          <el-tag type="warning" size="small">等待人工决策</el-tag>
          <el-tag size="small" type="info" effect="plain">指纹 {{ approval.payload_hash }}</el-tag>
          <el-tag size="small" type="info" effect="plain">v{{ approval.version }}</el-tag>
        </div>
      </div>
    </template>

    <el-alert
      :title="isPlan ? 'Planner 已生成执行计划，批准后才会派发执行（消耗 token）' : '报告已通过 A2A 审核，终审批准后输出为最终报告'"
      type="warning"
      show-icon
      :closable="false"
      class="approval-alert"
    />

    <!-- 待审内容：计划步骤 / 报告预览（编辑态切换为可编辑文本框） -->
    <div v-if="!editing">
      <div v-if="isPlan" class="plan-preview">
        <div v-for="step in planSteps" :key="step.id" class="plan-step">
          <el-tag size="small" :type="step.agent === 'researcher' ? 'primary' : 'success'" effect="plain">
            {{ step.agent === 'researcher' ? '搜索' : '撰写' }}
          </el-tag>
          <span class="plan-task">步骤 {{ step.id }}：{{ step.task }}</span>
          <span v-if="step.depends_on?.length" class="plan-deps">依赖 {{ step.depends_on.join(', ') }}</span>
        </div>
      </div>
      <div v-else class="report-preview" v-html="reportHtml" />
    </div>
    <el-input
      v-else
      v-model="editText"
      type="textarea"
      :rows="isPlan ? 10 : 14"
      :placeholder="isPlan ? '编辑计划 JSON（步骤数组，agent 仅可取 researcher/writer）' : '编辑报告 Markdown'"
      class="edit-area"
    />

    <!-- 决策区 -->
    <div class="decision-bar">
      <el-input v-model="approver" class="approver-input" size="small" placeholder="审批人">
        <template #prepend>审批人</template>
      </el-input>
      <el-input v-model="comment" class="comment-input" size="small" placeholder="审批意见（退回时必填）" />
    </div>
    <div class="action-bar">
      <template v-if="!editing">
        <el-button type="success" :loading="submitting" @click="submit('approve')">批准</el-button>
        <el-button type="primary" plain @click="startEdit">修改</el-button>
        <el-button type="danger" plain :loading="submitting" @click="submit('reject')">退回</el-button>
      </template>
      <template v-else>
        <el-button type="primary" :loading="submitting" @click="submit('edit')">改后批准</el-button>
        <el-button @click="editing = false">放弃修改</el-button>
      </template>
    </div>
  </el-card>
</template>

<style scoped>
.approval-card {
  margin-bottom: 16px;
  border-color: #e6a23c;
}
.approval-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-title {
  font-weight: 600;
  font-size: 14px;
}
.approval-tags {
  display: flex;
  gap: 8px;
}
.approval-alert {
  margin-bottom: 12px;
}
.plan-preview {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.plan-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #303133;
}
.plan-deps {
  color: #909399;
  font-size: 12px;
}
.report-preview {
  max-height: 360px;
  overflow: auto;
  padding: 8px 12px;
  background: #fafafa;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.7;
}
.edit-area {
  font-family: monospace;
}
.decision-bar {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.approver-input {
  width: 220px;
  flex-shrink: 0;
}
.comment-input {
  flex: 1;
}
.action-bar {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
