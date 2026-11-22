import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 可派发文件分区清单 ----

const DISPATCH_BASE = '/api/compaction-dispatch'

async function dispatchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(`${DISPATCH_BASE}${path}`, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload?.detail ?? `派发接口请求失败（${response.status}）`)
  }
  return payload as T
}

export type CandidatePartition = {
  目标表: string
  分区取值: string
  分区文件总数: number
  分区总大小MB: number
  可领取小文件数: number
  超大文件数: number
  候选单元数: number
  候选单元: { 单元编号: string; 文件数: number; 总大小MB: number; 输入路径: string[] }[]
  待处理路径: string[]
  待处理原因: string[]
}

export type CandidatePreview = {
  目标表: string
  阈值: Record<string, number | string>
  分区清单: CandidatePartition[]
  候选单元总数: number
}

export type PlanUnit = {
  id: number
  单元编号: string
  目标表: string
  分区取值: string
  文件数: number
  总大小MB: number
  状态: string
  任务ID: string | null
  输入清单: { 文件路径: string }[]
  输出路径: string | null
  失败原因: string | null
  派发次数: number
}

export type CompactionPlan = {
  id: number
  计划编号: string
  目标表: string
  来源快照: string[]
  单元数: number
  文件总数: number
  状态: string
  备注: string
  工作单元: PlanUnit[]
  已替换路径: string[]
}

export function fetchCandidates(table: string): Promise<CandidatePreview> {
  return dispatchJson<CandidatePreview>(`/candidates?table=${encodeURIComponent(table)}`)
}

export function savePlan(
  table: string,
  units: { 分区取值: string; 输入路径: string[] }[],
  remark?: string,
): Promise<CompactionPlan> {
  return dispatchJson<CompactionPlan>('/plans', {
    method: 'POST',
    body: JSON.stringify({ table, units, remark: remark ?? null }),
  })
}

export function fetchPlans(table?: string): Promise<{ items: CompactionPlan[]; total: number }> {
  const query = table ? `?table=${encodeURIComponent(table)}` : ''
  return dispatchJson(`/plans${query}`)
}

export function fetchPlan(planId: number): Promise<CompactionPlan> {
  return dispatchJson<CompactionPlan>(`/plans/${planId}`)
}

export function dispatchPlan(planId: number, taskId?: string): Promise<Record<string, unknown>> {
  return dispatchJson(`/plans/${planId}/dispatch`, {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId ?? null }),
  })
}

export function reportReplacement(
  planId: number,
  unitId: number,
  payload: { output_path?: string; failed?: boolean; reason?: string },
): Promise<CompactionPlan> {
  return dispatchJson<CompactionPlan>(`/plans/${planId}/units/${unitId}/report`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function retryUnit(planId: number, unitId: number): Promise<Record<string, unknown>> {
  return dispatchJson(`/plans/${planId}/units/${unitId}/retry`, { method: 'POST' })
}
