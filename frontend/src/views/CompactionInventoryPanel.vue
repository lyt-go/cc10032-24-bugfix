<template>
  <section class="inventory-panel" data-testid="compaction-inventory-panel">
    <div class="panel-head">
      <h3>分区盘点回写与领取</h3>
      <button class="btn ghost" type="button" data-testid="refresh-claimable" @click="loadClaimable">
        刷新可领取
      </button>
    </div>

    <div class="inventory-grid">
      <label>
        <span>目标表</span>
        <input v-model="inventory.table" data-testid="inventory-table" />
      </label>
      <label>
        <span>分区</span>
        <input v-model="inventory.partition" data-testid="inventory-partition" />
      </label>
      <label>
        <span>有效文件数</span>
        <input v-model.number="inventory.file_count" type="number" min="0" data-testid="inventory-count" />
      </label>
      <label>
        <span>盘点编号</span>
        <input v-model="inventory.inventory_id" data-testid="inventory-id" />
      </label>
      <label>
        <span>版本</span>
        <input v-model="inventory.version" data-testid="inventory-version" />
      </label>
      <label>
        <span>盘点状态</span>
        <select v-model="inventory.status" data-testid="inventory-status">
          <option value="已确认">已确认/生效</option>
          <option value="已替换">已替换</option>
          <option value="已撤下">已撤下</option>
          <option value="待确认">待确认</option>
        </select>
      </label>
      <label class="check-label">
        <input v-model="inventory.confirmed" type="checkbox" data-testid="inventory-confirmed" />
        <span>已确认</span>
      </label>
      <button class="btn primary" type="button" data-testid="register-inventory" :disabled="busy" @click="registerInventory">
        登记盘点并回写
      </button>
    </div>

    <div class="inventory-grid replacement-grid">
      <label>
        <span>目标表</span>
        <input v-model="replacement.table" data-testid="replacement-table" />
      </label>
      <label>
        <span>撤下分区</span>
        <input v-model="replacement.old_partition" data-testid="replacement-old" />
      </label>
      <label>
        <span>接替分区</span>
        <input v-model="replacement.new_partition" data-testid="replacement-new" />
      </label>
      <label>
        <span>接替盘点文件数</span>
        <input v-model.number="replacement.file_count" type="number" min="0" data-testid="replacement-count" />
      </label>
      <button class="btn" type="button" data-testid="replace-partition" :disabled="busy" @click="replacePartition">
        撤下原分区并登记接替
      </button>
      <button class="btn" type="button" data-testid="sync-inventory" :disabled="busy" @click="syncInventory">
        回写全部盘点
      </button>
      <label>
        <span>领取表</span>
        <input v-model="claimTable" data-testid="claim-table" />
      </label>
      <button class="btn primary" type="button" data-testid="claim-next" :disabled="busy" @click="claimNext">
        领取下一个
      </button>
    </div>

    <p v-if="message" class="panel-message" data-testid="inventory-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="inventory-error">{{ errorMessage }}</p>

    <div class="claimable-list" data-testid="claimable-list">
      <strong>当前可领取：</strong>
      <span v-if="!claimable.length" class="empty-inline">暂无达到阈值且盘点有效的任务</span>
      <button
        v-for="item in claimable"
        :key="String(item.entry_id)"
        type="button"
        class="claim-chip"
        :data-testid="`claimable-${item.partition}`"
        @click="$emit('openDetail', Number(item.entry_id))"
      >
        {{ item.table }} / {{ item.partition }} / {{ item.file_count }}个
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { compactionRequest } from '@/api/backend-service'

const emit = defineEmits<{
  changed: []
  openDetail: [entryId: number]
}>()

const busy = ref(false)
const message = ref('')
const errorMessage = ref('')
const claimable = ref<Array<Record<string, any>>>([])
const claimTable = ref('验收表')

const inventory = reactive({
  table: '验收表',
  partition: 'A',
  file_count: 3,
  inventory_id: 'A-new',
  version: '2',
  status: '已确认',
  confirmed: true,
})

const replacement = reactive({
  table: '验收表',
  old_partition: 'A',
  new_partition: 'A2',
  file_count: 18,
})

async function loadClaimable() {
  try {
    const query = claimTable.value ? `?table=${encodeURIComponent(claimTable.value)}` : ''
    const data = await compactionRequest<{ items: Array<Record<string, any>> }>(`/claimable${query}`)
    claimable.value = data.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可领取列表读取失败'
  }
}

async function registerInventory() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await compactionRequest<Record<string, any>>('/inventories', { ...inventory })
    message.value = `盘点已登记，并回写 ${result.writeback?.updated_ids?.length ?? 0} 个待执行任务。`
    emit('changed')
    await loadClaimable()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记盘点失败'
  } finally {
    busy.value = false
  }
}

async function replacePartition() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    await compactionRequest('/replacements', { ...replacement })
    message.value = `已登记 ${replacement.old_partition} 撤下，由 ${replacement.new_partition} 接替。`
    emit('changed')
    await loadClaimable()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区接替登记失败'
  } finally {
    busy.value = false
  }
}

async function syncInventory() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await compactionRequest<Record<string, any>>('/sync', { create_missing: true })
    message.value = `回写完成：更新 ${result.updated_ids?.length ?? 0} 条，新建 ${result.created_ids?.length ?? 0} 条。`
    emit('changed')
    await loadClaimable()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '盘点回写失败'
  } finally {
    busy.value = false
  }
}

async function claimNext() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await compactionRequest<Record<string, any>>('/claim-next', { table: claimTable.value })
    if (!result.ok) throw new Error(String(result.reason ?? '暂无可领取任务'))
    message.value = `已领取任务 ${result.entry_id}：${result.partition}，按 ${result.file_count} 个文件执行。`
    emit('changed')
    await loadClaimable()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
  } finally {
    busy.value = false
  }
}

onMounted(loadClaimable)
</script>

<style scoped>
.inventory-panel {
  border: 1px solid #d0d5dd;
  border-radius: 12px;
  padding: 14px 16px;
  margin: 12px 0;
  background: #f8fafc;
}
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.panel-head h3 { margin: 0 0 10px; }
.inventory-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  align-items: end;
  margin-bottom: 10px;
}
.inventory-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: #344054; }
.inventory-grid input { padding: 7px 9px; border: 1px solid #d0d5dd; border-radius: 8px; }
.check-label { flex-direction: row !important; align-items: center; height: 34px; }
.replacement-grid { padding-top: 10px; border-top: 1px dashed #cbd5e1; }
.panel-message { color: #067647; margin: 6px 0; }
.claimable-list { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 13px; }
.claim-chip {
  border: 1px solid #98a2b3;
  background: white;
  border-radius: 999px;
  padding: 4px 10px;
  cursor: pointer;
}
.empty-inline { color: #667085; }
</style>
