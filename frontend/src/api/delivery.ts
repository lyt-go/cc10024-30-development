/** 快照数据集交付接口：按采集运行打包、发布、下载、导入回放与核对。 */
import { request } from './client'

export type DeliveryEntry = {
  entry_key: string
  snapshot_id: number
  snapshot_code: string
  source: string
  run_id: number | null
  page_version: string
  summary: string
  snapshot_time: string
  replay_level: 'full' | 'preview_only'
  has_body: boolean
  body_length: number
  reason: string
}

export type BackfillItem = {
  entry_key: string
  snapshot_id: number
  snapshot_code: string
  origin_run_id: number | null
  origin_run_code: string
  source: string
  page_version: string
  reason: string
  backfill_hint: string
}

export type DeliveryDataset = {
  dataset_id: string
  kind: string
  run_id: number | null
  run_code: string
  plan: string
  published_at: string
  entries: DeliveryEntry[]
  manifest: {
    entry_count: number
    full_count: number
    preview_only_count: number
    backfill_count: number
    body_checksum: string
    entry_index: { entry_key: string; replay_level: string; has_body: boolean }[]
  }
  backfill: BackfillItem[]
}

export type DeliveryRecord = {
  dataset_id: string
  run_id: number | null
  run_code: string
  published_at: string
  filename: string
  entry_count: number
  full_count: number
  preview_only_count: number
  backfill_count: number
  body_checksum: string
}

export type ImportReport = {
  dataset_id: string
  run_id: number | null
  imported_full: number
  backfill_required: number
  skipped_duplicates: string[]
  replayed: { entry_key: string; snapshot_code: string; summary: string; body_length: number }[]
  backfill: BackfillItem[]
  verify_messages: string[]
  imported_at: string
}

async function jsonOrThrow<T>(response: Response, fallback: string): Promise<T> {
  const payload = await response.json()
  if (!response.ok) throw new Error(payload?.detail ?? fallback)
  return payload as T
}

/** 构建数据集预览（不落盘），查看每个条目的正文/摘要/来源/理由。 */
export async function buildDeliveryDataset(input: {
  run_id: number
  snapshot_ids: number[]
  channel_id?: number
  remark?: string
}): Promise<{ ok: boolean; dataset: DeliveryDataset }> {
  const response = await request('/api/snapshot-delivery/build', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return jsonOrThrow(response, '数据集构建失败')
}

/** 导出通道写快照数据集：原子发布不可变交付文件。 */
export async function publishDeliveryDataset(input: {
  run_id: number
  snapshot_ids: number[]
  channel_id: number
  remark?: string
}): Promise<{ ok: boolean; delivery: { dataset_id: string; path: string; manifest: DeliveryDataset['manifest'] } }> {
  const response = await request('/api/snapshot-delivery/publish', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return jsonOrThrow(response, '数据集发布失败')
}

/** 交付登记索引：已发布的交付范围。 */
export async function listDeliveries(runId?: number): Promise<{ items: DeliveryRecord[]; total: number }> {
  const query = runId ? `?run_id=${runId}` : ''
  const response = await request(`/api/snapshot-delivery/deliveries${query}`)
  return jsonOrThrow(response, '交付记录读取失败')
}

/** 运行历史：已发布交付范围和待补清单。 */
export async function runDeliveryScope(runId: number) {
  const response = await request(`/api/snapshot-delivery/runs/${runId}/scope`)
  return jsonOrThrow<{
    run_id: number
    delivery_count: number
    deliveries: DeliveryRecord[]
    backfill: BackfillItem[]
    backfill_count: number
  }>(response, '运行交付范围读取失败')
}

/** 读取交付数据集全文。 */
export async function getDeliveryDataset(datasetId: string): Promise<DeliveryDataset> {
  const response = await request(`/api/snapshot-delivery/deliveries/${datasetId}`)
  return jsonOrThrow(response, '交付数据集读取失败')
}

/** 下载交付文件（独立可回放 JSON）。 */
export async function downloadDeliveryDataset(datasetId: string): Promise<void> {
  const response = await request(`/api/snapshot-delivery/deliveries/${datasetId}/download`)
  if (!response.ok) throw new Error('交付文件下载失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${datasetId}.json`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

/** 核对交付数据集：清单与实有正文是否一致。 */
export async function verifyDeliveryDataset(datasetId: string) {
  const response = await request(`/api/snapshot-delivery/deliveries/${datasetId}/verify`)
  return jsonOrThrow<{ ok: boolean; messages: string[] }>(response, '数据集核对失败')
}

/** 页面快照读取同一数据集：验正文并重建可回放条目视图。 */
export async function replayDeliveryEntry(datasetId: string, entryKey: string) {
  const response = await request(
    `/api/snapshot-delivery/deliveries/${datasetId}/entries/${encodeURIComponent(entryKey)}/replay`,
  )
  return jsonOrThrow<{
    replay_level: string
    has_body: boolean
    body_verified: boolean
    replayable: boolean
    summary: string
    source: string
    reason: string
  }>(response, '条目回放失败')
}

/** 接收方导入数据集（JSON 文本），重复导入幂等。 */
export async function importDeliveryDataset(datasetText: string): Promise<{ ok: boolean; report: ImportReport }> {
  const dataset = JSON.parse(datasetText)
  const response = await request('/api/snapshot-delivery/import', {
    method: 'POST',
    body: JSON.stringify({ dataset }),
  })
  return jsonOrThrow(response, '数据集导入失败')
}

/** 接收方按下载文件原样导入（请求体即文件字节）。 */
export async function importDeliveryFile(file: File): Promise<{ ok: boolean; report: ImportReport }> {
  const response = await request('/api/snapshot-delivery/import/file', {
    method: 'POST',
    body: await file.text(),
  })
  return jsonOrThrow(response, '交付文件导入失败')
}
