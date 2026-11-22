<template>
  <section class="page" data-module="snapshot_delivery">
    <header class="page-head">
      <div>
        <h2>快照数据集交付</h2>
        <p class="page-desc">
          按采集运行把页面快照打成自包含数据集：完整快照可独立回放，只有预览的旧档进入待补清单；
          交付文件发布后不受后续回收影响，清单与实有正文始终一致。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="open-import" type="button" @click="showImport = true">
          接收方导入回放
        </button>
        <button class="btn" data-testid="refresh" type="button" @click="reloadDeliveries">
          刷新交付记录
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">已发布交付</span>
        <strong class="stat-value">{{ deliveries.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">完整快照条目</span>
        <strong class="stat-value">{{ totalFull }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待补（仅预览旧档）</span>
        <strong class="stat-value">{{ totalBackfill }}</strong>
      </article>
    </div>

    <!-- 存档方：按运行构建并发布 -->
    <form class="filter-bar delivery-form" @submit.prevent="onBuild">
      <label class="filter-item">
        <span>采集运行 ID（run_history）</span>
        <input v-model.number="form.run_id" data-testid="input-run" type="number" min="1" required />
      </label>
      <label class="filter-item">
        <span>页面快照 ID（逗号分隔，留空取全部）</span>
        <input v-model="form.snapshot_ids_text" data-testid="input-snapshots" placeholder="如 1,2,3" />
      </label>
      <label class="filter-item">
        <span>导出通道 ID（须已启用）</span>
        <input v-model.number="form.channel_id" data-testid="input-channel" type="number" min="1" required />
      </label>
      <button class="btn" data-testid="build" type="submit">构建预览</button>
      <button class="btn primary" data-testid="publish" type="button" :disabled="!preview" @click="onPublish">
        发布交付
      </button>
    </form>
    <p v-if="message" class="page-desc" :data-testid="'message'">{{ message }}</p>

    <!-- 构建预览：每个条目说清正文/摘要/来源/理由 -->
    <div v-if="preview" class="preview-box" data-testid="preview">
      <h3>数据集预览 {{ preview.dataset_id }}（尚未发布）</h3>
      <p class="page-desc">
        共 {{ preview.manifest.entry_count }} 条：完整 {{ preview.manifest.full_count }} 条、
        仅预览待补 {{ preview.manifest.preview_only_count }} 条
      </p>
      <table class="data-table">
        <thead>
          <tr><th>快照编号</th><th>采集来源</th><th>摘要</th><th>正文</th><th>回放分级</th><th>理由</th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in preview.entries" :key="entry.entry_key" :data-level="entry.replay_level">
            <td>{{ entry.snapshot_code }}</td>
            <td>{{ entry.source }}</td>
            <td>{{ entry.summary }}</td>
            <td>{{ entry.has_body ? `完整正文 ${entry.body_length} 字符` : '无（仅预览）' }}</td>
            <td>
              <span :class="['tag', entry.replay_level === 'full' ? 'tag-full' : 'tag-preview']">
                {{ entry.replay_level === 'full' ? '完整可回放' : '仅预览待补' }}
              </span>
            </td>
            <td>{{ entry.reason }}</td>
          </tr>
        </tbody>
      </table>
      <h4>待补清单（按原运行及来源定位补采）</h4>
      <table v-if="preview.backfill.length" class="data-table">
        <thead><tr><th>快照编号</th><th>原运行</th><th>采集来源</th><th>补采提示</th></tr></thead>
        <tbody>
          <tr v-for="item in preview.backfill" :key="item.entry_key">
            <td>{{ item.snapshot_code }}</td>
            <td>{{ item.origin_run_id }} / {{ item.origin_run_code }}</td>
            <td>{{ item.source }}</td>
            <td>{{ item.backfill_hint }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="page-desc">无待补项，全部条目可完整回放。</p>
    </div>

    <!-- 已发布交付记录 -->
    <h3>已发布交付记录</h3>
    <table class="data-table" data-testid="delivery-table">
      <thead>
        <tr>
          <th>数据集编号</th><th>运行</th><th>发布时间</th><th>条目</th>
          <th>完整</th><th>待补</th><th>校验和</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="record in deliveries" :key="record.dataset_id" :data-testid="`row-${record.dataset_id}`">
          <td>{{ record.dataset_id }}</td>
          <td>{{ record.run_id }} / {{ record.run_code }}</td>
          <td>{{ record.published_at }}</td>
          <td>{{ record.entry_count }}</td>
          <td>{{ record.full_count }}</td>
          <td>{{ record.backfill_count }}</td>
          <td><code>{{ record.body_checksum.slice(0, 12) }}…</code></td>
          <td class="row-actions">
            <button class="link" type="button" @click="onInspect(record.dataset_id)">查看条目</button>
            <button class="link" type="button" @click="onVerify(record.dataset_id)">核对</button>
            <button class="link" type="button" @click="onDownload(record.dataset_id)">下载</button>
            <button class="link" type="button" @click="onScope(record.run_id)">交付范围</button>
          </td>
        </tr>
        <tr v-if="!deliveries.length">
          <td colspan="8" class="empty-state">暂无交付记录，先按运行构建并发布快照数据集</td>
        </tr>
      </tbody>
    </table>

    <!-- 条目详情 / 回放验证 -->
    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal" @click.self="detail = null">
      <div class="modal">
        <h3>数据集条目 {{ detail.dataset_id }}</h3>
        <table class="data-table">
          <thead><tr><th>快照编号</th><th>来源</th><th>摘要</th><th>分级</th><th>正文</th><th>回放验证</th><th>理由</th></tr></thead>
          <tbody>
            <tr v-for="entry in detail.entries" :key="entry.entry_key">
              <td>{{ entry.snapshot_code }}</td>
              <td>{{ entry.source }}</td>
              <td>{{ entry.summary }}</td>
              <td>{{ entry.replay_level === 'full' ? '完整可回放' : '仅预览待补' }}</td>
              <td>{{ entry.has_body ? `${entry.body_length} 字符` : '无' }}</td>
              <td>
                <button class="link" type="button" @click="onReplay(detail.dataset_id, entry.entry_key)">
                  验正文
                </button>
                <span v-if="replayResult[entry.entry_key]">
                  {{ replayResult[entry.entry_key].replayable ? '✓ 可回放' : '✗ 不可回放' }}
                </span>
              </td>
              <td>{{ entry.reason }}</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- 接收方导入 -->
    <div v-if="showImport" class="modal-backdrop" data-testid="import-modal" @click.self="showImport = false">
      <form class="modal" @submit.prevent="onImportText">
        <h3>接收方导入回放（重复导入幂等）</h3>
        <label class="create-filter">
          <span>选择交付文件（.json）</span>
          <input data-testid="import-file" type="file" accept="application/json,.json" @change="onImportFile" />
        </label>
        <p class="page-desc">或粘贴数据集 JSON 文本：</p>
        <textarea v-model="importText" data-testid="import-text" rows="8" class="import-text"></textarea>
        <p v-if="importReport" class="page-desc" data-testid="import-report">
          完整回放 {{ importReport.imported_full }} 条、待补 {{ importReport.backfill_required }} 条、
          重复跳过 {{ importReport.skipped_duplicates.length }} 条
        </p>
        <ul v-if="importReport" class="backfill-list">
          <li v-for="item in importReport.backfill" :key="item.entry_key">
            待补 {{ item.snapshot_code }}：{{ item.backfill_hint }}
          </li>
        </ul>
        <div class="page-actions">
          <button class="btn" type="button" @click="showImport = false">取消</button>
          <button class="btn primary" data-testid="import-submit" type="submit" :disabled="!importText.trim()">
            导入 JSON
          </button>
        </div>
      </form>
    </div>

    <!-- 运行交付范围 -->
    <div v-if="scope" class="modal-backdrop" data-testid="scope-modal" @click.self="scope = null">
      <div class="modal">
        <h3>运行 {{ scope.run_id }} 的交付范围</h3>
        <p class="page-desc">已发布 {{ scope.delivery_count }} 份交付，待补 {{ scope.backfill_count }} 项。</p>
        <ul class="backfill-list">
          <li v-for="d in scope.deliveries" :key="d.dataset_id">
            {{ d.dataset_id }}：完整 {{ d.full_count }} / 待补 {{ d.backfill_count }}（{{ d.published_at }}）
          </li>
        </ul>
        <h4>待补来源</h4>
        <ul class="backfill-list">
          <li v-for="(item, index) in scope.backfill" :key="`${item.snapshot_code}-${index}`">
            {{ item.snapshot_code }} ← {{ item.source }}（{{ item.backfill_hint }}）
          </li>
        </ul>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="scope = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>交付文件原子落盘，发布后回收不影响已交付正文</span>
      <span v-if="message" class="error-text">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  buildDeliveryDataset,
  downloadDeliveryDataset,
  getDeliveryDataset,
  importDeliveryDataset,
  importDeliveryFile,
  listDeliveries,
  publishDeliveryDataset,
  replayDeliveryEntry,
  runDeliveryScope,
  verifyDeliveryDataset,
} from '@/api/delivery'
import type { DeliveryDataset, DeliveryRecord, ImportReport } from '@/api/delivery'

const form = ref({ run_id: 1, snapshot_ids_text: '', channel_id: 3 })
const preview = ref<DeliveryDataset | null>(null)
const deliveries = ref<DeliveryRecord[]>([])
const detail = ref<DeliveryDataset | null>(null)
const scope = ref<Awaited<ReturnType<typeof runDeliveryScope>> | null>(null)
const showImport = ref(false)
const importText = ref('')
const importReport = ref<ImportReport | null>(null)
const replayResult = ref<Record<string, Awaited<ReturnType<typeof replayDeliveryEntry>>>>({})
const message = ref('')

const totalFull = computed(() => deliveries.value.reduce((sum, r) => sum + r.full_count, 0))
const totalBackfill = computed(() => deliveries.value.reduce((sum, r) => sum + r.backfill_count, 0))

function parseSnapshotIds(): number[] | undefined {
  const text = form.value.snapshot_ids_text.trim()
  if (!text) return undefined
  return text.split(',').map((part) => Number(part.trim())).filter((n) => Number.isInteger(n) && n > 0)
}

async function onBuild() {
  message.value = ''
  try {
    const res = await buildDeliveryDataset({
      run_id: form.value.run_id,
      snapshot_ids: parseSnapshotIds() ?? [],
      channel_id: form.value.channel_id,
    })
    preview.value = res.dataset
    message.value = '数据集已构建预览，确认条目无误后可发布交付'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '构建失败'
  }
}

async function onPublish() {
  if (!preview.value) return
  message.value = ''
  try {
    const res = await publishDeliveryDataset({
      run_id: form.value.run_id,
      snapshot_ids: parseSnapshotIds() ?? [],
      channel_id: form.value.channel_id,
    })
    message.value = `已发布 ${res.delivery.dataset_id}，文件可独立回放`
    preview.value = null
    await reloadDeliveries()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '发布失败'
  }
}

async function reloadDeliveries() {
  try {
    const res = await listDeliveries()
    deliveries.value = res.items
  } catch (error) {
    message.value = error instanceof Error ? error.message : '交付记录读取失败'
  }
}

async function onInspect(datasetId: string) {
  message.value = ''
  try {
    detail.value = await getDeliveryDataset(datasetId)
    replayResult.value = {}
  } catch (error) {
    message.value = error instanceof Error ? error.message : '条目读取失败'
  }
}

async function onVerify(datasetId: string) {
  try {
    const res = await verifyDeliveryDataset(datasetId)
    message.value = res.ok ? `核对通过：${res.messages.join('；')}` : `核对失败：${res.messages.join('；')}`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '核对失败'
  }
}

async function onDownload(datasetId: string) {
  try {
    await downloadDeliveryDataset(datasetId)
    message.value = `交付文件 ${datasetId}.json 已下载，可独立回放`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '下载失败'
  }
}

async function onScope(runId: number | null) {
  if (runId == null) return
  try {
    scope.value = await runDeliveryScope(runId)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '交付范围读取失败'
  }
}

async function onReplay(datasetId: string, entryKey: string) {
  try {
    replayResult.value[entryKey] = await replayDeliveryEntry(datasetId, entryKey)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回放验证失败'
  }
}

async function onImportFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    const res = await importDeliveryFile(file)
    importReport.value = res.report
    importText.value = await file.text()
    message.value = `文件导入完成：完整回放 ${res.report.imported_full} 条`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '文件导入失败'
  }
}

async function onImportText() {
  try {
    const res = await importDeliveryDataset(importText.value)
    importReport.value = res.report
    message.value = `导入完成：完整回放 ${res.report.imported_full} 条，重复跳过 ${res.report.skipped_duplicates.length} 条`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '导入失败'
  }
}

onMounted(reloadDeliveries)
</script>

<style scoped>
.delivery-form { align-items: flex-end; }
.preview-box { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 12px 0; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.tag-full { background: #dcfce7; color: #166534; }
.tag-preview { background: #fef3c7; color: #92400e; }
.import-text { width: 100%; font-family: monospace; font-size: 12px; }
.backfill-list { font-size: 13px; padding-left: 18px; }
.backfill-list li { margin: 4px 0; }
.create-filter span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.row-actions .link { margin-right: 8px; }
</style>
