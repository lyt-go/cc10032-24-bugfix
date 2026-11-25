<template>
  <div class="modal-backdrop" data-testid="compaction-detail-modal" @click.self="$emit('close')">
    <section class="modal detail-modal" role="dialog" aria-modal="true" aria-label="压缩任务详情">
      <div class="detail-head">
        <h3>任务详情：{{ detail?.['任务编号'] ?? entryId }}</h3>
        <button class="link" type="button" data-testid="close-detail" @click="$emit('close')">关闭</button>
      </div>

      <p v-if="loading">正在读取任务盘点依据…</p>
      <p v-else-if="loadError" class="error-text" data-testid="detail-load-error">{{ loadError }}</p>

      <template v-else-if="detail">
        <dl class="detail-grid">
          <div><dt>目标表</dt><dd data-testid="detail-table">{{ detail['目标表'] }}</dd></div>
          <div><dt>目标分区</dt><dd data-testid="detail-partition">{{ detail['目标分区'] }}</dd></div>
          <div><dt>当前状态</dt><dd data-testid="detail-status">{{ detail.status }}</dd></div>
          <div><dt>任务文件计数</dt><dd data-testid="detail-file-count">{{ detail['文件数量'] }}</dd></div>
          <div class="wide">
            <dt>盘点依据</dt>
            <dd data-testid="inventory-basis">{{ detail.inventory_basis }}</dd>
          </div>
          <div class="wide">
            <dt>采用范围</dt>
            <dd data-testid="adoption-scope">
              {{ scopeDisplay }}
            </dd>
          </div>
          <div class="wide">
            <dt>生效盘点</dt>
            <dd data-testid="effective-inventory">
              <span v-if="detail.effective_inventory">
                {{ detail.effective_inventory.inventory_id }}@{{ detail.effective_inventory.version }}
                ／ {{ detail.effective_inventory.file_count }}个文件
                ／ {{ detail.effective_inventory.status }}
              </span>
              <span v-else>当前分区无有效盘点；历史任务按保存的文件计数复核。</span>
            </dd>
          </div>
          <div v-if="replacementPartition" class="wide replacement-note" data-testid="replacement-note">
            原分区已撤下，可采用接替分区：<strong>{{ replacementPartition }}</strong>
            <button class="btn" type="button" data-testid="adopt-replacement" :disabled="busy" @click="adoptReplacement(true)">
              采用 {{ replacementPartition }} 并执行
            </button>
          </div>
          <div v-if="actionMessage" class="wide"><dd data-testid="detail-action-message">{{ actionMessage }}</dd></div>
          <div v-if="actionError" class="wide"><dd class="error-text" data-testid="detail-action-error">{{ actionError }}</dd></div>
        </dl>

        <div class="detail-actions">
          <button class="btn" type="button" data-testid="detail-precheck" :disabled="busy" @click="precheck">预检盘点</button>
          <button class="btn primary" type="button" data-testid="detail-execute" :disabled="busy" @click="execute">发起压缩</button>
          <button class="btn ghost" type="button" @click="loadDetail">重新进入/刷新</button>
        </div>

        <section class="history-section">
          <h4>复核记录</h4>
          <ul v-if="history.length" class="history-list" data-testid="attempt-history">
            <li v-for="(item, index) in history" :key="index">
              <span>{{ item.at }}</span>
              <strong>{{ item.stage }}</strong>
              <span v-if="item.file_count !== undefined">读数 {{ item.file_count }}</span>
              <span v-if="item.replacement_partition">接替 {{ item.replacement_partition }}</span>
              <span :class="item.ok ? 'ok-text' : 'error-text'">{{ item.reason || (item.ok ? '通过' : '未执行') }}</span>
            </li>
          </ul>
          <p v-else>暂无预检/执行记录。已完成历史任务仍保留其原始采用盘点。</p>
        </section>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { compactionRequest } from '@/api/backend-service'

const props = defineProps<{ entryId: number }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const detail = ref<Record<string, any> | null>(null)
const loading = ref(true)
const loadError = ref('')
const busy = ref(false)
const actionMessage = ref('')
const actionError = ref('')

const history = computed<Array<Record<string, any>>>(() => detail.value?.attempt_history ?? [])
const replacementPartition = computed<string>(() =>
  detail.value?.replacement_partition
  ?? detail.value?.effective_resolution?.replacement?.new_partition
  ?? detail.value?.effective_resolution?.conflict?.replacement_partition
  ?? '',
)
const scopeDisplay = computed<string>(() => {
  if (!detail.value) return '尚未执行/预检'
  const chineseScope = detail.value['采用范围']
  if (typeof chineseScope === 'string' && chineseScope) return chineseScope
  if (detail.value.adoption_scope) return formatScope(detail.value.adoption_scope)
  if (detail.value.planned_scope) return formatScope(detail.value.planned_scope)
  return '尚未执行/预检'
})

function formatScope(scope: Record<string, any> | undefined): string {
  if (!scope) return ''
  return `${scope.table ?? ''} / ${scope.partition ?? ''} / ${scope.inventory_id ?? '未记录盘点'} / ${scope.version ?? '历史版本'} / ${scope.file_count ?? ''}个文件`
}

async function loadDetail() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await compactionRequest<{ entry: Record<string, any> }>(`/${props.entryId}/detail`)
    detail.value = data.entry
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '任务详情读取失败'
  } finally {
    loading.value = false
  }
}

async function precheck() {
  busy.value = true
  actionError.value = ''
  actionMessage.value = ''
  try {
    const result = await compactionRequest<Record<string, any>>(`/${props.entryId}/precheck`, {})
    actionMessage.value = String(result.reason || (result.claimable ? '预检通过，可领取执行' : '预检未给出可领取结论'))
    await loadDetail()
    emit('changed')
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '预检失败'
  } finally {
    busy.value = false
  }
}

async function execute() {
  busy.value = true
  actionError.value = ''
  actionMessage.value = ''
  try {
    const result = await compactionRequest<Record<string, any>>(`/${props.entryId}/execute`, {})
    actionMessage.value = String(result.reason || (result.ok ? `执行完成：${result.files_before} → ${result.files_after}` : '本次未执行'))
    if (!result.ok) actionError.value = actionMessage.value
    await loadDetail()
    emit('changed')
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '执行失败'
  } finally {
    busy.value = false
  }
}

async function adoptReplacement(runExecute: boolean) {
  busy.value = true
  actionError.value = ''
  try {
    const result = await compactionRequest<Record<string, any>>(
      `/${props.entryId}/adopt-replacement`,
      { execute: runExecute },
    )
    actionMessage.value = String(result.reason || '已采用接替分区')
    await loadDetail()
    emit('changed')
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '采用接替分区失败'
  } finally {
    busy.value = false
  }
}

onMounted(loadDetail)
</script>

<style scoped>
.detail-modal { width: min(760px, 100%); }
.detail-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px; margin: 12px 0; }
.detail-grid .wide { grid-column: 1 / -1; }
.detail-grid dt { font-size: 12px; color: #667085; }
.detail-grid dd { margin: 2px 0 0; font-size: 14px; word-break: break-all; }
.replacement-note {
  background: #fffaeb;
  border: 1px solid #fedf89;
  border-radius: 8px;
  padding: 10px;
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.detail-actions { display: flex; gap: 8px; margin: 12px 0; }
.history-section { border-top: 1px solid #e4e7ec; padding-top: 10px; }
.history-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }
.history-list li { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; align-items: center; }
.ok-text { color: #067647; }
</style>
