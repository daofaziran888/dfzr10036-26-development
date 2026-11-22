<template>
  <section class="page" data-module="compaction-dispatch">
    <header class="page-head">
      <div>
        <h2>可派发文件分区清单</h2>
        <p class="page-desc">
          按文件大小与分区阈值生成合并候选，工作单元不跨分区、不跨表；
          已替换路径不再选入，失败重试只回到本单元尚未确认的范围。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reload" @click="reloadAll">刷新清单</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reloadCandidates">
      <label class="filter-item">
        <span>目标表</span>
        <select v-model="table" data-testid="table-select" @change="reloadAll">
          <option v-for="item in tables" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit">生成候选清单</button>
      <label class="filter-item">
        <span>计划备注</span>
        <input v-model="remark" data-testid="remark" placeholder="可选，保存计划时记录" />
      </label>
    </form>

    <p v-if="preview" class="threshold-banner" data-testid="threshold-banner">
      容量阈值（含等号）：文件数 {{ preview.阈值['最小文件数'] }}～{{ preview.阈值['最大文件数'] }} 个，
      总大小 {{ preview.阈值['最小总大小MB'] }}～{{ preview.阈值['最大总大小MB'] }} MB；
      候选单元 {{ preview.候选单元总数 }} 个
    </p>
    <p v-if="message" class="error-text" data-testid="dispatch-message">{{ message }}</p>

    <h3>一、按分区候选（勾选后保存为合并计划）</h3>
    <article v-for="part in preview?.分区清单 ?? []" :key="part.分区取值" class="partition-card"
             :data-testid="`partition-${part.分区取值}`">
      <header class="partition-head">
        <strong>{{ part.分区取值 }}</strong>
        <span>文件 {{ part.分区文件总数 }} 个 / 合计 {{ part.分区总大小MB }}MB /
          候选单元 {{ part.候选单元数 }} 个 / 超大文件 {{ part.超大文件数 }} 个</span>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>选入</th><th>候选单元</th><th>文件数</th><th>总大小</th><th>输入路径</th></tr>
        </thead>
        <tbody>
          <tr v-for="unit in part.候选单元" :key="unit.单元编号"
              :data-testid="`candidate-${unit.单元编号}`">
            <td>
              <input type="checkbox" :value="unitKey(part, unit)" v-model="selected"
                     :data-testid="`select-${unit.单元编号}`" />
            </td>
            <td>{{ unit.单元编号 }}</td>
            <td :class="boundaryClass(unit.文件数, maxFiles)">{{ unit.文件数 }}</td>
            <td :class="boundaryClass(unit.总大小MB, maxSize)">{{ unit.总大小MB }}MB</td>
            <td class="path-cell">{{ unit.输入路径.join('、') }}</td>
          </tr>
          <tr v-if="!part.候选单元.length">
            <td colspan="5" class="empty-state">该分区没有满足容量阈值的候选单元</td>
          </tr>
        </tbody>
      </table>
      <p v-if="part.待处理原因.length" class="pending-note">
        仍待处理 {{ part.待处理路径.length }} 个路径：{{ part.待处理原因.join('；') }}
      </p>
      <div class="row-actions">
        <button class="btn" type="button" @click="selectPartition(part, true)">全选本分区</button>
        <button class="btn ghost" type="button" @click="selectPartition(part, false)">清空本分区</button>
      </div>
    </article>

    <div class="page-actions save-bar">
      <button class="btn primary" type="button" data-testid="save-plan" :disabled="!selected.length"
              @click="saveSelected">
        保存为合并计划（{{ selected.length }} 个单元）
      </button>
    </div>

    <h3>二、已保存计划与派发回执</h3>
    <article v-for="plan in plans" :key="plan.id" class="partition-card"
             :data-testid="`plan-${plan.id}`">
      <header class="partition-head">
        <strong>{{ plan.计划编号 }} · {{ plan.目标表 }} · {{ plan.状态 }}</strong>
        <span>单元 {{ plan.单元数 }} 个 / 文件 {{ plan.文件总数 }} 个 /
          来源快照 {{ plan.来源快照.join(',') }}</span>
        <button class="btn primary" type="button" :data-testid="`dispatch-${plan.id}`"
                @click="doDispatch(plan)">派发待领取单元</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>单元</th><th>分区</th><th>容量</th><th>状态</th><th>任务</th>
            <th>冻结输入清单（历史可核对）</th><th>替换输出 / 失败原因</th><th>回执操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="unit in plan.工作单元" :key="unit.id"
              :data-testid="`unit-${unit.id}`" :data-status="unit.状态">
            <td>{{ unit.单元编号 }}<br /><small>派发 {{ unit.派发次数 }} 次</small></td>
            <td>{{ unit.分区取值 }}</td>
            <td>{{ unit.文件数 }} 个 / {{ unit.总大小MB }}MB</td>
            <td>{{ unit.状态 }}</td>
            <td>{{ unit.任务ID ?? '—' }}</td>
            <td class="path-cell">
              <div v-for="f in unit.输入清单" :key="f.文件路径" class="frozen-path">{{ f.文件路径 }}</div>
            </td>
            <td class="path-cell">
              <div v-if="unit.输出路径" class="frozen-path">→ {{ unit.输出路径 }}</div>
              <div v-if="unit.失败原因" class="error-text">{{ unit.失败原因 }}</div>
              <span v-if="!unit.输出路径 && !unit.失败原因">—</span>
            </td>
            <td>
              <template v-if="unit.状态 === '已派发'">
                <input v-model="outputs[unit.id]" :data-testid="`output-${unit.id}`"
                       class="output-input" :placeholder="defaultOutput(unit)" />
                <div class="row-actions">
                  <button class="btn primary" type="button"
                          :data-testid="`report-ok-${unit.id}`"
                          @click="doReport(plan, unit, false)">回传成功</button>
                  <button class="btn" type="button"
                          :data-testid="`report-fail-${unit.id}`"
                          @click="doReport(plan, unit, true)">回传失败</button>
                </div>
              </template>
              <button v-else-if="unit.状态 === '失败'" class="btn primary" type="button"
                      :data-testid="`retry-${unit.id}`" @click="doRetry(plan, unit)">
                失败重试（限本单元未确认范围）
              </button>
              <span v-else class="muted">无可用动作</span>
            </td>
          </tr>
        </tbody>
      </table>
    </article>
    <p v-if="!plans.length" class="empty-state">暂无合并计划，请先在上方勾选候选单元并保存</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  dispatchPlan,
  fetchCandidates,
  fetchPlans,
  reportReplacement,
  retryUnit,
  savePlan,
} from '@/api/backend-service'
import type { CandidatePartition, CandidatePreview, CompactionPlan, PlanUnit } from '@/api/backend-service'

const tables = ['ads.order_events_iceberg', 'ods.payment_log_iceberg']
const table = ref(tables[0])
const preview = ref<CandidatePreview | null>(null)
const plans = ref<CompactionPlan[]>([])
const selected = ref<string[]>([])
const remark = ref('')
const message = ref('')
const outputs = ref<Record<number, string>>({})

const maxFiles = 5
const maxSize = 100

function unitKey(part: CandidatePartition, unit: { 单元编号: string }): string {
  return `${part.分区取值}::${unit.单元编号}`
}

function boundaryClass(value: number, limit: number): string {
  return value === limit ? 'at-boundary' : ''
}

function selectPartition(part: CandidatePartition, checked: boolean) {
  const keys = part.候选单元.map((u) => unitKey(part, u))
  const next = new Set(selected.value)
  keys.forEach((k) => (checked ? next.add(k) : next.delete(k)))
  selected.value = [...next]
}

async function reloadCandidates() {
  message.value = ''
  try {
    preview.value = await fetchCandidates(table.value)
    selected.value = []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '候选清单读取失败'
  }
}

async function reloadPlans() {
  try {
    plans.value = (await fetchPlans(table.value)).items
  } catch (error) {
    message.value = error instanceof Error ? error.message : '计划列表读取失败'
  }
}

async function reloadAll() {
  await Promise.all([reloadCandidates(), reloadPlans()])
}

async function saveSelected() {
  if (!preview.value) return
  const units = preview.value.分区清单.flatMap((part) =>
    part.候选单元
      .filter((u) => selected.value.includes(unitKey(part, u)))
      .map((u) => ({ 分区取值: part.分区取值, 输入路径: u.输入路径 })),
  )
  try {
    await savePlan(table.value, units, remark.value)
    message.value = `合并计划已保存并原子领取 ${units.length} 个工作单元`
    await reloadAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '计划保存失败'
  }
}

async function doDispatch(plan: CompactionPlan) {
  try {
    const result = await dispatchPlan(plan.id)
    message.value = `派发完成：新增 ${String(result.派发数)} 个任务，跳过 ${
      (result.跳过单元 as unknown[]).length
    } 个已发出/已完成单元`
    await reloadAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '派发失败'
  }
}

function defaultOutput(unit: PlanUnit): string {
  const first = unit.输入清单[0]?.文件路径 ?? ''
  const dir = first.includes('/') ? first.slice(0, first.lastIndexOf('/') + 1) : ''
  return `${dir}compacted-${unit.单元编号}.parquet`
}

async function doReport(plan: CompactionPlan, unit: PlanUnit, failed: boolean) {
  const output = (outputs.value[unit.id] ?? '').trim() || defaultOutput(unit)
  try {
    await reportReplacement(plan.id, unit.id, failed
      ? { failed: true, reason: '执行器回报失败，未产生替换' }
      : { output_path: output })
    message.value = failed
      ? `单元 ${unit.单元编号} 已回传失败，文件释放为本计划可重试范围`
      : `单元 ${unit.单元编号} 替换成功，同分区下次候选将剔除这些路径`
    await reloadAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回执失败'
  }
}

async function doRetry(plan: CompactionPlan, unit: PlanUnit) {
  try {
    const result = await retryUnit(plan.id, unit.id)
    message.value = `重试已派发，范围 ${(result.重试范围 as string[]).length} 个冻结输入路径`
    await reloadAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '重试失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.threshold-banner {
  background: #eff8ff;
  border: 1px solid #b2ddff;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
}
.partition-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 14px;
}
.partition-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 13px;
}
.path-cell {
  font-size: 12px;
  max-width: 360px;
  word-break: break-all;
}
.frozen-path {
  margin-bottom: 2px;
}
.pending-note {
  font-size: 12px;
  color: #b54708;
  margin: 8px 0;
}
.save-bar {
  margin: 12px 0 18px;
}
.output-input {
  width: 220px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px;
  margin-bottom: 6px;
}
.at-boundary {
  color: #b54708;
  font-weight: 700;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px;
  min-width: 240px;
}
</style>
