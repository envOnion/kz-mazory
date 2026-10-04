<template>
  <div class="panel space-y-3" data-testid="chart-kpi">
    <h3 class="font-semibold">{{ data.title }}</h3>
    <p class="text-xs text-slate-400">{{ data.unit }} · Подтверждённые значения</p>
    <div class="h-72 relative"><Bar :data="chart" :options="options" /></div>
    <details><summary class="text-xs text-indigo-300 cursor-pointer">Таблица значений</summary>
      <div class="overflow-auto max-h-56"><table class="data-table"><thead><tr><th>Показатель</th><th v-for="series in data.datasets" :key="series.label">{{ series.label }}</th></tr></thead><tbody><tr v-for="(label, index) in data.labels" :key="index"><td>{{ label }}</td><td v-for="series in data.datasets" :key="series.label">{{ series.data[index] ?? 'Нет данных' }}</td></tr></tbody></table></div>
    </details>
  </div>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { Chart as ChartJS, Tooltip, Legend, BarElement, CategoryScale, LinearScale, type ChartOptions, type ChartData } from 'chart.js'
import { Bar } from 'vue-chartjs'
import type { ChartPayload } from '../../types/chat'
ChartJS.register(Tooltip, Legend, BarElement, CategoryScale, LinearScale)
const props = defineProps<{ data: ChartPayload }>()
// The only money-to-number adapter: chart visualization never changes API identifiers or exact table values.
const chart = computed<ChartData<'bar'>>(() => ({ labels: props.data.labels, datasets: props.data.datasets.map(series => ({ ...series, data: series.data.map(value => value === null ? null : Number(value)), backgroundColor: series.backgroundColor || '#818cf8' })) }))
const options: ChartOptions<'bar'> = { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#cbd5e1' } } }, scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' }, beginAtZero: true } } }
</script>
