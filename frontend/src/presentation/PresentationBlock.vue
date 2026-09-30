<template>
  <article class="panel min-w-0 space-y-3" :class="block.size === 'wide' ? 'md:col-span-2' : ''">
    <h2 class="font-semibold">{{ block.title || dataset.definition }}</h2>
    <p class="text-xs" :class="dataset.coverage.status === 'partial' ? 'text-amber-300' : 'text-emerald-300'">{{ dataset.coverage.message }}</p>
    <p v-if="dataset.truncated" class="text-xs text-amber-300">Показано {{ dataset.returned_count }} из {{ dataset.total_groups }} групп (top-N).</p>
    <p v-if="error" role="alert" class="text-rose-300">{{ error }}</p>
    <p v-else-if="!dataset.rows.length" class="text-slate-400">Нет данных по выбранным условиям.</p>
    <strong v-else-if="block.kind === 'kpi'" class="block text-3xl">{{ display(dataset.rows[0]?.[block.encoding.value!] ?? null, dataset.columns.find(c => c.name === block.encoding.value)?.unit) }}</strong>
    <div v-else-if="block.kind !== 'table'" ref="chartElement" class="h-80 w-full" role="img" :aria-label="block.title || dataset.definition" />
    <details v-if="block.kind !== 'table' && dataset.rows.length"><summary class="text-xs text-indigo-300 cursor-pointer">Данные графика</summary><DataTable :dataset="dataset" /></details>
    <DataTable v-if="block.kind === 'table'" :dataset="dataset" :columns="block.columns" />
    <div v-if="dataset.evidence.length" class="text-xs text-indigo-300 flex flex-wrap gap-2"><button v-for="source in dataset.evidence" :key="source.id" class="underline" @click="$emit('openSource', source.id)">Переписка проекта #{{ source.id }} · {{ source.sender_name }}</button></div>
    <details class="text-xs text-slate-400"><summary class="cursor-pointer">Условия и определение</summary><p class="mt-2">{{ dataset.definition }}</p><p>{{ dataset.timezone }}</p><p class="mt-2">{{ conditions }}</p></details>
  </article>
</template>
<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { init, use, type EChartsType } from 'echarts/core'
import { BarChart, LineChart, ScatterChart, PieChart, FunnelChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { PresentationBlock } from '../types/presentation'
import type { AnalyticsDataset } from '../types/analytics'
import { chartOptions, display, fieldLabel } from './options'
import DataTable from './DataTable.vue'
use([BarChart, LineChart, ScatterChart, PieChart, FunnelChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, CanvasRenderer])
defineEmits<{ openSource: [id: number] }>()
const props = defineProps<{ block: PresentationBlock; dataset: AnalyticsDataset }>()
const conditions = computed(() => {
  const q = props.dataset.normalized_query
  const dates = q.date_range as { start?: string; end_exclusive?: string } | null
  const filters = Array.isArray(q.filters) ? q.filters as { field: string; op: string; value: unknown }[] : []
  const operators: Record<string, string> = { eq: '=', in: 'в списке', gt: '>', gte: '≥', lt: '<', lte: '≤' }
  return [q.currency, dates ? `С ${dates.start} до ${dates.end_exclusive} (не включая)` : '', ...filters.map(f => `${fieldLabel(f.field)} ${operators[f.op] || f.op} ${String(f.value)}`)].filter(Boolean).join(' · ')
})
const chartElement = ref<HTMLDivElement>()
const error = ref('')
let chart: EChartsType | undefined
let observer: ResizeObserver | undefined
function render() {
  chart?.dispose(); chart = undefined; observer?.disconnect(); observer = undefined; error.value = ''
  if (!chartElement.value || ['table', 'kpi'].includes(props.block.kind) || !props.dataset.rows.length) return
  try { chart = init(chartElement.value); chart.setOption(chartOptions(props.block, props.dataset)); observer = new ResizeObserver(() => chart?.resize()); observer.observe(chartElement.value) }
  catch { chart?.dispose(); chart = undefined; error.value = 'Не удалось отрисовать этот график. Данные доступны в таблице.' }
}
onMounted(render)
watch(() => [props.block, props.dataset], render, { flush: 'post' })
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>
