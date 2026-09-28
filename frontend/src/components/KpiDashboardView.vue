<template>
  <section class="w-full max-w-6xl mx-auto px-4 py-6 space-y-5" aria-label="Показатели и ответы">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div><p class="text-xs text-indigo-300">ПОДТВЕРЖДЁННЫЕ ДАННЫЕ</p><h1 class="text-2xl font-semibold">KPI отдела продаж</h1></div>
      <select aria-label="Период" class="field" :value="period" @change="$emit('changePeriod', ($event.target as HTMLSelectElement).value)">
        <option value="this_month">Текущий месяц</option><option value="last_month">Прошлый месяц</option><option value="quarter">Квартал</option><option value="year">Год</option>
      </select>
    </div>
    <div class="flex flex-wrap gap-3 text-sm">
      <label>Команда <select class="field" v-model="filters.team_id"><option :value="undefined">Все доступные</option><option v-for="team in directory.teams" :key="team.id" :value="team.id">{{ team.name }}</option></select></label>
      <label>Менеджер <select class="field" v-model="filters.manager_id"><option :value="undefined">Все доступные</option><option v-for="profile in directory.profiles" :key="profile.id" :value="profile.id">{{ profile.full_name }}</option></select></label>
      <label>Проект <select class="field" v-model="filters.project_id"><option :value="undefined">Все доступные</option><option v-for="project in directory.projects" :key="project.id" :value="project.id">{{ project.name }}</option></select></label>
      <label>Валюта <select class="field" v-model="filters.currency"><option>KZT</option><option>USD</option><option>EUR</option><option>RUB</option></select></label>
    </div>
    <p v-if="isLoading" role="status" class="panel animate-pulse">Обрабатываю запрос…</p>
    <p v-if="responseText" class="panel whitespace-pre-wrap" data-testid="chat-response">{{ responseText }}</p>
    <template v-if="widget">
      <PresetChart v-if="widget.type === 'chart'" :data="widget.data" />
      <PresetCommitmentList v-else-if="widget.type === 'commitments_list'" :data="widget.data" />
      <PresetProjectTable v-else-if="widget.type === 'project_table'" :data="widget.data" />
    </template>
    <template v-if="data && (!widget || widget.type === 'kpi_grid')">
      <p class="text-sm text-slate-400">{{ data.querySubtitle }} · {{ data.updatedAtText }}</p>
      <p class="panel" :class="data.coverage.status === 'partial' ? 'text-amber-300' : 'text-emerald-300'">{{ data.coverage.message }}</p>
      <div class="grid md:grid-cols-3 gap-4">
        <button v-for="metric in data.summaryMetrics" :key="metric.id" class="panel text-left hover:border-indigo-400" @click="detail = metric.id">
          <span class="text-sm text-slate-400">{{ metric.title }}</span><strong class="block text-2xl mt-2">{{ metric.value }}</strong><span class="block text-xs text-slate-400 mt-2">{{ metric.trend }} · Детализация</span>
        </button>
      </div>
      <div v-if="detail" class="panel overflow-auto">
        <button class="btn float-right" @click="detail = ''">Закрыть детализацию</button>
        <h2 class="font-semibold mb-3">{{ detail === 'overdue' ? 'График погашения' : detail === 'target' ? 'Планы менеджеров' : 'Операции периода' }}</h2>
        <table v-if="detail === 'receipts'" class="data-table"><thead><tr><th>Дата</th><th>Проект</th><th>Сумма</th></tr></thead><tbody><tr v-for="row in paymentRows" :key="row.id"><td>{{ row.payment_date }}</td><td>#{{ row.project_id }}</td><td>{{ row.amount }} {{ row.currency }}</td></tr></tbody></table>
        <table v-else-if="detail === 'overdue'" class="data-table"><thead><tr><th>Срок</th><th>Проект</th><th>Осталось</th></tr></thead><tbody><tr v-for="row in data.receivables.rows" :key="row.id"><td>{{ row.due_date }}</td><td>#{{ row.project_id }}</td><td>{{ row.remaining }} {{ data.currency }}</td></tr></tbody></table>
        <p v-else v-for="manager in data.managers" :key="manager.id">{{ manager.name }}: {{ manager.targetFormatted }}</p>
        <p v-if="detail === 'receipts'" class="text-xs text-slate-400 mt-3">Загружено {{ paymentRows.length }} из {{ data.source_count }} операций. <button v-if="paymentRows.length < data.source_count" class="btn" @click="loadPayments">Загрузить ещё</button></p>
        <p class="text-xs text-slate-400 mt-3">{{ data.definition }}</p>
      </div>
      <div class="grid md:grid-cols-2 gap-5"><PresetChart :data="data.chartData" /><PresetChart :data="data.timeline" /></div>
      <PresetChart :data="agingChart" v-if="agingChart" />
      <div class="grid md:grid-cols-3 gap-4"><ManagerCard v-for="manager in data.managers" :key="manager.id" :manager="manager" /></div>
      <p v-if="!data.managers.length" class="panel">Нет доступных показателей. Доступ к команде и полноту источников настраивает администратор.</p>
      <div class="panel"><h2 class="font-semibold">Прогноз поступлений</h2><p v-if="data.forecast.available">{{ data.forecast.amount }} {{ data.forecast.currency }} · на {{ data.forecast.as_of }}</p><p class="text-xs text-slate-400">{{ data.forecast.reason }}</p></div>
    </template>
  </section>
</template>
<script setup lang="ts">
import { ref, computed, reactive, watch, onMounted } from 'vue'
import type { KpiFilters, Directory, Page, Payment } from '../types/platform'
import { api } from '../composables/api'
import type { KpiDashboardData, ChatWidget, ChartPayload } from '../types/chat'
import PresetChart from './presets/PresetChart.vue'
import PresetCommitmentList from './presets/PresetCommitmentList.vue'
import PresetProjectTable from './presets/PresetProjectTable.vue'
import ManagerCard from './ManagerCard.vue'
const props = defineProps<{ data: KpiDashboardData | null; widget: ChatWidget | null; responseText: string; isLoading: boolean; period: string }>()
const emit = defineEmits<{ changePeriod: [period: string]; changeFilters: [filters: KpiFilters]; selectPrompt: [prompt: string] }>()
const filters = reactive<KpiFilters>({ currency: 'KZT' })
const directory = ref<Directory>({ teams: [], profiles: [], projects: [] })
watch(filters, value => emit('changeFilters', { ...value }), { deep: true })
onMounted(async () => { try { directory.value = await api<Directory>('/directory/') } catch { /* Global request state reports authentication errors. */ } })
const detail = ref('')
const paymentRows = ref<Payment[]>([])
const paymentPage = ref(1)
watch(() => props.data, value => { paymentRows.value = value?.source_rows || []; paymentPage.value = 1 }, { immediate: true })
async function loadPayments() { if (props.data) { const page = await api<Page<Payment>>(`${props.data.source_path}&page=${paymentPage.value + 1}`); paymentRows.value.push(...page.results); paymentPage.value++ } }
const agingChart = computed<ChartPayload | null>(() => props.data ? ({ title: 'Дебиторка по возрасту', unit: props.data.currency, labels: ['Срок не наступил', '1–30 дней', '31–60 дней', '61–90 дней', 'Более 90 дней'], datasets: [{ label: 'Непогашенная сумма', data: ['not_due', '1_30', '31_60', '61_90', 'over_90'].map(key => props.data!.receivables.buckets[key] || '0.00'), backgroundColor: '#fbbf24' }] }) : null)
</script>
