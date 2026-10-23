<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Activity, AlertCircle, AlertTriangle, ArrowDownRight, ArrowUpRight, CheckCircle2, CircleHelp, Clock3, RefreshCw, XCircle } from 'lucide-vue-next'
import AiAnalyticsPanel from './AiAnalyticsPanel.vue'
import { api } from '../composables/api'
import { EmptyState, ErrorState } from './bits'
import type { AnalyticsOverview, OverviewDetails, OverviewItem, OverviewMetric, OverviewSource } from '../types/overview'
import type { AnalyticsOperations } from '../types/messenger'

type Period = 'today' | '7d' | '30d' | 'all' | 'custom'
const period = ref<Period>('today')
const fromDate = ref('')
const toDate = ref('')
const teamId = ref<number | null>(null)
const chatId = ref<number | null>(null)
const projectTimezone = ref('UTC')
const availableTeams = ref<AnalyticsOverview['filters']['teams']>([])
const availableChats = ref<AnalyticsOverview['filters']['chats']>([])
const overview = ref<AnalyticsOverview | null>(null)
const overviewError = ref('')
const loading = ref(true)
const stale = ref(false)
const details = ref<OverviewDetails | null>(null)
const detailsError = ref('')
const detailsLoading = ref(false)
const detailPage = ref(1)
const detailMetric = ref('')
const technical = ref<AnalyticsOperations | null>(null)
const technicalError = ref('')
const technicalLoading = ref(false)
let overviewController: AbortController | null = null
let detailController: AbortController | null = null
let generation = 0
let timer: number | undefined
let mounted = false

const periodOptions: { value: Period; label: string }[] = [
  { value: 'today', label: 'Сегодня' }, { value: '7d', label: '7 дней' }, { value: '30d', label: '30 дней' },
  { value: 'all', label: 'Весь период' }, { value: 'custom', label: 'Диапазон' },
]
const dateBoundsValid = computed(() => period.value !== 'custom' || Boolean(fromDate.value && toDate.value && fromDate.value <= toDate.value))
const sourceTone = (status: OverviewSource['status']) => ({ fresh: 'mx-chip-ok', stale: 'mx-chip-warn', unknown: 'mx-chip-muted', disconnected: 'mx-chip-err' })[status]
const sourceLabel = (status: OverviewSource['status']) => ({ fresh: 'Актуален', stale: 'Есть задержка', unknown: 'Неизвестно', disconnected: 'Нет связи' })[status]
const statusTone = computed(() => {
  if (overview.value?.status.code === 'attention') return overview.value.status.technical_issues > 0 ? 'mx-chip-err' : 'mx-chip-warn'
  return ({ empty: 'mx-chip-muted', unknown: 'mx-chip-warn', processing: 'mx-chip-info', attention: 'mx-chip-warn', complete: 'mx-chip-ok' } as const)[overview.value?.status.code ?? 'unknown']
})
const statusIcon = computed(() => {
  if (overview.value?.status.code === 'attention' && overview.value.status.technical_issues > 0) return XCircle
  return ({ empty: CircleHelp, unknown: CircleHelp, processing: Activity, attention: AlertTriangle, complete: CheckCircle2 } as const)[overview.value?.status.code ?? 'unknown']
})
const statusIconTone = computed(() => {
  if (overview.value?.status.code === 'attention') return overview.value.status.technical_issues > 0 ? 'text-rose-300' : 'text-amber-200'
  return ({ empty: 'text-slate-400', unknown: 'text-amber-200', processing: 'text-blue-300', attention: 'text-amber-200', complete: 'text-emerald-300' } as const)[overview.value?.status.code ?? 'unknown']
})
const freshnessText = computed(() => stale.value ? 'Не удалось обновить — показаны предыдущие данные' : 'Данные обновляются каждые 30 секунд, пока открыт этот экран')
const businessSummary = computed(() => {
  const metrics = overview.value?.outcomes.flatMap(outcome => outcome.metrics) ?? []
  const labels: Record<string, string> = { 'deal.created': 'Создано сделок', 'deal.updated': 'Обновлено сделок', 'task.created': 'Создано задач' }
  return ['deal.created', 'deal.updated', 'task.created']
    .map(key => metrics.find(metric => metric.key === key))
    .filter((metric): metric is OverviewMetric => metric !== undefined)
    .map(metric => ({ ...metric, label: labels[metric.key] ?? metric.label }))
})
const sourceSummary = computed(() => {
  const sources = overview.value?.sources ?? []
  if (!sources.length) return 'Источники: данных нет'
  const count = (status: OverviewSource['status']) => sources.filter(source => source.status === status).length
  return `Источники: ${count('fresh')} актуальны · ${count('stale')} с задержкой · ${count('unknown')} неизвестно · ${count('disconnected')} без связи`
})
const metricDefinitions = computed(() => {
  if (!overview.value) return []
  return [
    ...overview.value.messages.map(metric => ({ key: `message-${metric.key}`, title: metric.label, description: metric.description })),
    ...overview.value.dialogues.map(metric => ({ key: `dialogue-${metric.key}`, title: metric.label, description: metric.description })),
    ...overview.value.outcomes.flatMap(outcome => [
      { key: `outcome-${outcome.key}`, title: outcome.label, description: outcome.description },
      ...outcome.metrics.map(metric => ({ key: `outcome-metric-${metric.key}`, title: metric.label, description: metric.description })),
    ]),
    ...overview.value.attention.map(issue => ({ key: `issue-${issue.key}`, title: issue.label, description: issue.description })),
    ...overview.value.signals.map(signal => ({ key: `signal-${signal.key}`, title: signal.title, description: signal.description })),
  ]
})

function paramsForOverview(): URLSearchParams {
  const params = new URLSearchParams({ period: period.value })
  if (period.value === 'custom' && fromDate.value && toDate.value) {
    params.set('from', dateAtProjectMidnight(fromDate.value))
    params.set('to', dateAtProjectMidnight(nextCalendarDate(toDate.value)))
  }
  if (teamId.value !== null) params.set('team', String(teamId.value))
  if (chatId.value !== null) params.set('chat', String(chatId.value))
  return params
}

function dateAtProjectMidnight(date: string): string {
  const [year, month, day] = date.split('-').map(Number)
  const target = Date.UTC(year, month - 1, day)
  let guess = target
  const timezone = projectTimezone.value
  for (let iteration = 0; iteration < 2; iteration++) {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(guess))
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]))
    const represented = Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day), Number(values.hour), Number(values.minute), Number(values.second))
    guess += target - represented
  }
  return new Date(guess).toISOString()
}
function nextCalendarDate(date: string): string {
  const [year, month, day] = date.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10)
}

async function loadOverview(silent = false) {
  if (!dateBoundsValid.value) return
  overviewController?.abort()
  const controller = new AbortController()
  overviewController = controller
  const requestGeneration = ++generation
  if (!silent || !overview.value) loading.value = true
  overviewError.value = ''
  try {
    const result = await api<AnalyticsOverview>(`/analytics/overview/?${paramsForOverview()}`, { signal: controller.signal })
    if (requestGeneration !== generation || controller.signal.aborted) return
    overview.value = result
    projectTimezone.value = result.timezone
    availableTeams.value = result.filters.teams
    availableChats.value = result.filters.chats
    stale.value = false
    if (details.value) await loadDetails(detailPage.value, detailMetric.value, result.as_of)
  } catch (error) {
    if (controller.signal.aborted || requestGeneration !== generation) return
    overviewError.value = error instanceof Error ? error.message : 'Не удалось загрузить обзор.'
    stale.value = Boolean(overview.value)
  } finally {
    if (requestGeneration === generation) loading.value = false
  }
}

async function loadDetails(page: number, metric: string, asOf = overview.value?.as_of) {
  if (!asOf) return
  detailController?.abort()
  const controller = new AbortController()
  detailController = controller
  const requestedMetric = metric
  detailsLoading.value = true
  detailsError.value = ''
  const params = paramsForOverview()
  params.set('as_of', asOf)
  params.set('metric', metric)
  params.set('page', String(page))
  try {
    const result = await api<OverviewDetails>(`/analytics/overview/details/?${params}`, { signal: controller.signal })
    if (!controller.signal.aborted && requestedMetric === detailMetric.value) {
      details.value = result
      detailPage.value = result.page
    }
  } catch (error) {
    if (!controller.signal.aborted) detailsError.value = error instanceof Error ? error.message : 'Не удалось загрузить детали.'
  } finally {
    if (!controller.signal.aborted) detailsLoading.value = false
  }
}

function openDetails(metric: string) {
  detailMetric.value = metric
  detailPage.value = 1
  details.value = null
  void loadDetails(1, metric)
  nextTickScroll()
}
function nextTickScroll() {
  window.setTimeout(() => document.getElementById('overview-details')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
}
function closeDetails() { detailController?.abort(); details.value = null; detailsError.value = ''; detailsLoading.value = false; detailMetric.value = '' }
function activityHref(item: OverviewItem) { return item.href || item.source_href || item.crm_href }
function stateLabel(state: string) {
  const labels: Record<string, string> = {
    received: 'Получено', queued: 'В очереди', enqueued: 'В очереди', pending: 'Ожидает', analyzing: 'Анализируется', processing: 'Выполняется',
    need_context: 'Нужен контекст', awaiting: 'Ожидает ответа', answered: 'Ответ получен', clarification: 'Уточнение',
    analyzed: 'Проанализировано', no_crm_actions: 'Без действий CRM', non_commercial: 'Без изменений CRM', confirmed: 'Подтверждено', delivered: 'Доставлено',
    completed: 'Завершено', done: 'Выполнено', failed: 'Ошибка', error: 'Ошибка', expired: 'Истекло', stale: 'Задержка',
    unknown: 'Результат неизвестен', unavailable: 'Результат недоступен', superseded: 'Заменено', cancelled: 'Отменено',
    approved: 'Одобрено', open: 'Открыто', ready: 'Готово', partial: 'Частично выполнено', blocked: 'Заблокировано', unconfirmed: 'Не подтверждено',
  }
  return labels[state] ?? state.replaceAll('_', ' ')
}
function goToPage(page: number) { if (page > 0 && detailMetric.value) void loadDetails(page, detailMetric.value) }
function visibleMetric(metric: OverviewMetric) { return metric.value !== null }
function metricDisplay(metric: OverviewMetric) { return metric.value === null ? 'Нет данных' : new Intl.NumberFormat('ru-RU').format(metric.value) }
function formatDuration(seconds: number | null) {
  if (seconds === null) return 'Нет данных'
  if (seconds < 60) return `${Math.round(seconds)} сек.`
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин.`
  return `${(seconds / 3600).toFixed(1).replace('.', ',')} ч`
}
function formatPercent(value: number | null) { return value === null ? 'Нет данных' : `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value)}%` }
function formatOverviewTime(iso: string | null | undefined) {
  if (!iso) return 'Время неизвестно'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return 'Время неизвестно'
  const timezone = projectTimezone.value
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
  const dateText = new Intl.DateTimeFormat('ru-RU', { timeZone: timezone, day: '2-digit', month: 'short' }).format(date)
  const timeText = new Intl.DateTimeFormat('ru-RU', { timeZone: timezone, hour: '2-digit', minute: '2-digit' }).format(date)
  const dateKey = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(date)
  return dateKey === today ? timeText : `${dateText} ${timeText}`
}
function signalIcon(severity: string) { return severity === 'danger' ? XCircle : severity === 'warning' ? AlertTriangle : CircleHelp }

async function loadTechnical() {
  technicalLoading.value = true; technicalError.value = ''
  try { technical.value = await api<AnalyticsOperations>('/analytics/operations/') }
  catch (error) { technicalError.value = error instanceof Error ? error.message : 'Не удалось загрузить технические сведения.' }
  finally { technicalLoading.value = false }
}
function syncTimer() {
  if (timer !== undefined) window.clearInterval(timer)
  timer = undefined
  if (mounted && document.visibilityState === 'visible') timer = window.setInterval(() => { void loadOverview(true) }, 30_000)
}
function onVisibilityChange() { syncTimer(); if (document.visibilityState === 'visible' && mounted) void loadOverview(true) }

watch([period, fromDate, toDate, teamId, chatId], () => {
  overviewController?.abort(); generation++
  details.value = null; detailMetric.value = ''; detailsError.value = ''; detailsLoading.value = false
  detailController?.abort()
  overview.value = null; stale.value = false
  if (!dateBoundsValid.value) { overview.value = null; loading.value = false; return }
  void loadOverview()
})
watch(teamId, () => { chatId.value = null }, { flush: 'sync' })

onMounted(() => {
  mounted = true
  document.addEventListener('visibilitychange', onVisibilityChange)
  void loadOverview()
  syncTimer()
})
onBeforeUnmount(() => {
  mounted = false; generation++
  overviewController?.abort(); detailController?.abort()
  if (timer !== undefined) window.clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <main class="p-4 md:p-5 flex flex-col gap-4 max-w-7xl mx-auto" aria-label="Обзор аналитики">
    <section class="mx-card p-3 md:p-4" aria-label="Фильтры аналитики">
      <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-2 items-end">
        <label class="text-xs mx-muted">Период
          <select v-model="period" class="mx-select mt-1" aria-label="Период аналитики"><option v-for="option in periodOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select>
        </label>
        <label v-if="period === 'custom'" class="text-xs mx-muted">С даты
          <input v-model="fromDate" type="date" class="mx-input mt-1" aria-label="Начало периода" />
        </label>
        <label v-if="period === 'custom'" class="text-xs mx-muted">По дату включительно
          <input v-model="toDate" type="date" class="mx-input mt-1" aria-label="Конец периода" />
        </label>
        <label class="text-xs mx-muted">Команда
          <select v-model="teamId" class="mx-select mt-1" aria-label="Фильтр по команде"><option :value="null">Все доступные команды</option><option v-for="team in availableTeams" :key="team.id" :value="team.id">{{ team.name }}</option></select>
        </label>
        <label class="text-xs mx-muted">Чат
          <select v-model="chatId" class="mx-select mt-1" aria-label="Фильтр по чату"><option :value="null">Все доступные чаты</option><option v-for="chat in availableChats.filter(row => teamId === null || row.team_id === null || row.team_id === teamId)" :key="chat.id" :value="chat.id">{{ chat.name }}</option></select>
        </label>
      </div>
      <div v-if="period === 'custom' && !dateBoundsValid" role="alert" class="text-xs mt-2" style="color:var(--mx-warn)">Укажите корректные даты: начало не позже конца.</div>
      <p v-if="period === 'custom'" class="mx-muted text-xs mt-2">Границы дат заданы по часовому поясу {{ projectTimezone }}.</p>
    </section>

    <div v-if="loading && !overview" class="mx-card p-5 space-y-3" role="status" aria-live="polite">
      <p class="mx-muted">Загружаем состояние аналитики…</p><div class="mx-skeleton h-16" /><div class="mx-skeleton h-24" />
    </div>
    <ErrorState v-else-if="overviewError && !overview" :text="overviewError" :on-retry="() => loadOverview()" />
    <EmptyState v-else-if="!overview" title="Нет доступных данных" text="Обзор пока не получил данные для выбранных условий." />

    <template v-else>
      <div v-if="overviewError" role="alert" class="mx-card mx-chip-warn p-3 flex items-start gap-2">
        <AlertCircle :size="16" class="mt-0.5 shrink-0" /><span>{{ freshnessText }} · {{ overviewError }}</span><button class="mx-link ml-auto" @click="loadOverview()">Повторить</button>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs mx-muted" aria-live="polite">
        <span>Срез на {{ formatOverviewTime(overview.as_of) }} · часовой пояс {{ overview.timezone }} · {{ overview.coverage === 'scoped' ? 'в границах вашего доступа' : overview.coverage }}</span>
        <span class="flex items-center gap-2"><span>{{ freshnessText }}</span><button class="mx-btn" :disabled="loading" aria-label="Обновить обзор" @click="loadOverview()"><RefreshCw :size="14" /> Обновить</button></span>
      </div>

      <section class="mx-card p-4 md:p-5" :class="stale ? 'border-amber-300/40' : ''" aria-labelledby="overview-state-title">
        <div class="flex flex-col md:flex-row md:items-start gap-4 justify-between">
          <div class="flex gap-3 min-w-0">
            <component :is="statusIcon" :size="22" class="mt-1 shrink-0" :class="statusIconTone" aria-hidden="true" />
            <div><div class="flex items-center gap-2 flex-wrap"><h2 id="overview-state-title" class="text-lg font-semibold">{{ overview.status.title }}</h2><span class="mx-chip" :class="statusTone">{{ overview.status.code === 'complete' ? 'Подтверждено' : overview.status.code === 'processing' ? 'В работе' : overview.status.code === 'attention' ? 'Нужны действия' : overview.status.code === 'empty' ? 'Пока пусто' : 'Недостаточно подтверждений' }}</span></div>
              <p class="mx-muted mt-1 max-w-3xl">{{ overview.status.description }}</p>
              <p v-if="overview.status.processed_through" class="mx-muted text-xs mt-2">Обработано по {{ formatOverviewTime(overview.status.processed_through) }}</p>
              <div v-if="businessSummary.length" class="grid grid-cols-1 sm:grid-cols-3 gap-2 mt-4">
                <button v-for="metric in businessSummary" :key="metric.key" class="mx-card px-3 py-2 text-left hover:brightness-125" :title="metric.description" :disabled="!visibleMetric(metric)" @click="openDetails(metric.key)">
                  <span class="mx-muted text-xs block">{{ metric.label }}</span><span class="text-lg font-semibold tabular-nums">{{ metricDisplay(metric) }}</span><span class="sr-only">{{ metric.description }}</span>
                </button>
              </div>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-2 min-w-[210px] text-sm">
            <div class="mx-card p-3"><div class="mx-muted text-xs">Технические проблемы</div><div class="font-semibold" :class="overview.status.technical_issues ? 'text-rose-300' : ''">{{ overview.status.technical_issues }}</div></div>
            <div class="mx-card p-3"><div class="mx-muted text-xs">Ждут ответа</div><div class="font-semibold">{{ overview.status.human_questions }}</div></div>
          </div>
        </div>
        <details class="mt-4 border-t pt-3" style="border-color:var(--mx-border)">
          <summary class="text-xs uppercase tracking-wide mx-muted cursor-pointer">{{ sourceSummary }}</summary>
          <div v-if="overview.sources.length" class="grid sm:grid-cols-2 xl:grid-cols-3 gap-2 mt-3">
            <article v-for="source in overview.sources" :key="source.id" class="mx-card p-3 min-w-0">
              <div class="flex items-center justify-between gap-2"><strong class="truncate">{{ source.name }}</strong><span class="mx-chip shrink-0" :class="sourceTone(source.status)">{{ sourceLabel(source.status) }}</span></div>
              <p class="mx-muted text-xs mt-2">Проверка: {{ formatOverviewTime(source.checked_at) }} · покрытие по {{ formatOverviewTime(source.covered_through) }}</p>
              <p class="mx-muted text-xs mt-1">Последнее сообщение: {{ formatOverviewTime(source.last_message_at) }}</p>
              <p v-if="source.description" class="text-xs mt-2">{{ source.description }}</p>
            </article>
          </div>
          <p v-else class="mx-muted text-sm mt-3">Источники в этой выборке не доступны; свежесть не подтверждена.</p>
        </details>
      </section>

      <section class="mx-card p-4" aria-labelledby="outcome-title">
        <div class="flex flex-wrap items-end justify-between gap-2 mb-3"><div><h2 id="outcome-title" class="font-semibold">Результаты для бизнеса</h2><p class="mx-muted text-xs mt-1">Локальные проекты, сделки и подтверждённые действия CRM показаны раздельно.</p></div><p class="mx-muted text-xs">{{ overview.comparison }}</p></div>
        <div class="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
          <article v-for="outcome in overview.outcomes" :key="outcome.key" class="mx-card p-3">
            <h3 class="font-medium" :title="outcome.description">{{ outcome.label }}<span class="sr-only">{{ outcome.description }}</span></h3>
            <div class="space-y-2 mt-3"><button v-for="metric in outcome.metrics" :key="metric.key" class="w-full flex items-baseline justify-between gap-3 text-left hover:text-[var(--mx-accent-2)] disabled:opacity-60" :title="metric.description" :disabled="!visibleMetric(metric)" @click="openDetails(metric.key)"><span class="text-sm">{{ metric.label }}<span class="sr-only">{{ metric.description }}</span></span><span class="font-semibold tabular-nums shrink-0" :class="metric.tone === 'danger' ? 'text-rose-300' : metric.tone === 'warning' ? 'text-amber-200' : ''">{{ metricDisplay(metric) }}</span></button></div>
          </article>
        </div>
      </section>

      <section class="grid lg:grid-cols-2 gap-4" aria-label="Результаты обработки">
        <article class="mx-card p-4">
          <h2 class="font-semibold">Входящие сообщения</h2><p class="mx-muted text-xs mt-1 mb-3">Каждая цифра раскрывает элементы этой же выборки.</p>
          <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
            <button v-for="metric in overview.messages" :key="metric.key" class="mx-card p-3 text-left hover:brightness-125 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--mx-accent)]" :title="metric.description" :disabled="!visibleMetric(metric)" @click="openDetails(metric.key)">
              <span class="mx-muted text-xs block">{{ metric.label }}</span><span class="text-xl font-semibold block mt-1">{{ metricDisplay(metric) }}</span><span class="sr-only">{{ metric.description }}</span>
            </button>
          </div>
        </article>
        <article class="mx-card p-4">
          <h2 class="font-semibold">Обсуждения и темы</h2><p class="mx-muted text-xs mt-1 mb-3">Диалоги считаются отдельно от сообщений.</p>
          <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
            <button v-for="metric in overview.dialogues" :key="metric.key" class="mx-card p-3 text-left hover:brightness-125 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--mx-accent)]" :title="metric.description" :disabled="!visibleMetric(metric)" @click="openDetails(metric.key)">
              <span class="mx-muted text-xs block">{{ metric.label }}</span><span class="text-xl font-semibold block mt-1">{{ metricDisplay(metric) }}</span><span class="sr-only">{{ metric.description }}</span>
            </button>
          </div>
        </article>
      </section>

      <details v-if="metricDefinitions.length" class="mx-card p-4">
        <summary class="font-medium cursor-pointer">Как считаются показатели</summary>
        <dl class="mt-3 grid md:grid-cols-2 gap-x-5 gap-y-3">
          <div v-for="definition in metricDefinitions" :key="definition.key"><dt class="font-medium text-sm">{{ definition.title }}</dt><dd class="mx-muted text-xs mt-1">{{ definition.description }}</dd></div>
        </dl>
      </details>

      <section class="grid lg:grid-cols-2 gap-4" aria-label="Требующие внимания вопросы и сигналы">
        <article class="mx-card p-4"><h2 class="font-semibold">Что требует внимания</h2><p class="mx-muted text-xs mt-1 mb-3">Группы могут пересекаться, поэтому их нельзя суммировать.</p>
          <div v-if="overview.attention.length" class="space-y-2"><button v-for="issue in overview.attention" :key="issue.key" class="mx-card w-full p-3 text-left flex gap-3 items-start hover:brightness-125" @click="openDetails(issue.key)"><AlertTriangle :size="17" class="shrink-0 mt-0.5" :class="issue.severity === 'danger' ? 'text-rose-300' : 'text-amber-200'" /><span class="flex-1"><strong>{{ issue.label }}</strong><span class="mx-muted text-xs block mt-1">{{ issue.description }}</span></span><span class="text-lg font-semibold tabular-nums">{{ issue.count }}</span></button></div>
          <p v-else class="mx-muted text-sm">Нет групп вопросов для внимания в доступных данных.</p>
        </article>
        <article class="mx-card p-4"><h2 class="font-semibold">Изменения и сигналы</h2><p class="mx-muted text-xs mt-1 mb-3">Сигнал показывает основание и ведёт к элементам, на которых он рассчитан.</p>
          <div v-if="overview.signals.length" class="space-y-2"><button v-for="signal in overview.signals" :key="signal.key" class="mx-card w-full p-3 text-left flex gap-3 items-start hover:brightness-125" @click="openDetails(signal.metric)"><component :is="signalIcon(signal.severity)" :size="17" class="shrink-0 mt-0.5" :class="signal.severity === 'danger' ? 'text-rose-300' : signal.severity === 'warning' ? 'text-amber-200' : 'text-blue-300'" /><span><strong>{{ signal.title }}</strong><span class="mx-muted text-xs block mt-1">{{ signal.description }}</span></span></button></div>
          <p v-else class="mx-muted text-sm">Сигналов для выбранной выборки нет.</p>
        </article>
      </section>

      <section class="grid md:grid-cols-3 gap-3" aria-label="Скорость обработки">
        <article class="mx-card p-4"><div class="mx-muted text-xs flex items-center gap-2"><Clock3 :size="14" /> Старейшее незавершённое</div><div class="mt-2 font-semibold">{{ formatOverviewTime(overview.performance.oldest_pending_at) }}</div></article>
        <article class="mx-card p-4"><div class="mx-muted text-xs flex items-center gap-2"><ArrowUpRight :size="14" /> Медиана до подтверждения CRM</div><div class="mt-2 font-semibold">{{ formatDuration(overview.performance.median_delivery_seconds) }}</div><p class="mx-muted text-xs mt-1">Выборка: {{ overview.performance.sample_size }}</p></article>
        <article class="mx-card p-4"><div class="mx-muted text-xs flex items-center gap-2"><ArrowDownRight :size="14" /> Без участия человека</div><div class="mt-2 font-semibold">{{ formatPercent(overview.performance.automatic_percent) }}</div></article>
      </section>

      <section class="mx-card p-4" aria-labelledby="activity-title"><div class="flex justify-between gap-2 items-start"><div><h2 id="activity-title" class="font-semibold">Последние результаты</h2><p class="mx-muted text-xs mt-1">Переходы доступны только при наличии ссылки и прав.</p></div><Activity :size="17" class="mx-muted" /></div>
        <div v-if="overview.activity.length" class="mt-3 divide-y" style="border-color:var(--mx-border)"><article v-for="item in overview.activity" :key="item.id" class="py-3 flex flex-wrap justify-between gap-2"><div><strong>{{ item.label }}</strong><p class="mx-muted text-xs mt-1">{{ item.description }}</p><div class="flex gap-3 mt-1 text-xs"><a v-if="item.href" :href="item.href" class="mx-link">Факт / результат →</a><a v-if="item.source_href" :href="item.source_href" class="mx-link">Исходное сообщение →</a><a v-if="item.crm_href" :href="item.crm_href" class="mx-link">CRM →</a></div></div><span class="mx-muted text-xs shrink-0">{{ formatOverviewTime(item.at) }} · {{ stateLabel(item.state) }}</span></article></div>
        <p v-else class="mx-muted text-sm mt-3">В выбранной выборке нет доступных элементов ленты.</p>
      </section>

      <section v-if="overview.limitations.length" class="mx-card p-4"><h2 class="font-semibold">Ограничения данных</h2><ul class="list-disc pl-5 mt-2 space-y-1 mx-muted text-sm"><li v-for="(limitation, index) in overview.limitations" :key="index">{{ limitation }}</li></ul></section>

      <section v-if="details || detailsLoading || detailsError" id="overview-details" class="mx-card p-4 scroll-mt-24" aria-labelledby="details-title" aria-live="polite">
        <div class="flex justify-between gap-3 items-start"><div><h2 id="details-title" class="font-semibold">{{ details?.title ?? 'Детали показателя' }}</h2><p v-if="details" class="mx-muted text-xs mt-1">{{ details.count }} элементов · срез {{ formatOverviewTime(details.as_of) }}</p><p v-if="details" class="mx-muted text-xs mt-1">Состав сообщений ограничен временем среза; состояния обновляются при загрузке.</p></div><button class="mx-btn" @click="closeDetails">Закрыть</button></div>
        <p v-if="detailsLoading" role="status" class="mx-muted mt-4">Загружаем детали…</p>
        <ErrorState v-else-if="detailsError" :text="detailsError" :on-retry="() => loadDetails(detailPage, detailMetric)" />
        <div v-else-if="details" class="mt-3">
          <div v-if="details.items.length" class="divide-y" style="border-color:var(--mx-border)"><article v-for="item in details.items" :key="item.id" class="py-3 flex flex-wrap justify-between gap-2"><div><strong>{{ item.label }}</strong><p class="mx-muted text-xs mt-1">{{ item.description }}</p><a v-if="activityHref(item)" :href="activityHref(item) ?? undefined" class="mx-link text-xs inline-block mt-2">Открыть основание →</a></div><span class="mx-muted text-xs">{{ formatOverviewTime(item.at) }} · {{ stateLabel(item.state) }}</span></article></div>
          <p v-else class="mx-muted py-4">В этой выборке нет элементов для выбранного показателя.</p>
          <div class="flex items-center justify-between gap-2 mt-3"><button class="mx-btn" :disabled="detailsLoading || detailPage <= 1" @click="goToPage(detailPage - 1)">Назад</button><span class="mx-muted text-xs">Страница {{ detailPage }}</span><button class="mx-btn" :disabled="detailsLoading || details.next_page === null" @click="goToPage(details.next_page ?? detailPage + 1)">Дальше</button></div>
        </div>
      </section>

      <details class="mx-card p-4"><summary class="cursor-pointer font-medium">Техническая сводка и финансовая AI-аналитика</summary><div class="mt-4 space-y-4">
        <div class="flex flex-wrap justify-between items-center gap-2"><div><h2 class="font-semibold">Операции · все доступные данные</h2><p class="mx-muted text-xs mt-1">Сводка прежнего API не ограничена выбранным периодом или фильтрами обзора.</p></div><button class="mx-btn" :disabled="technicalLoading" @click="loadTechnical"><RefreshCw :size="13" />{{ technical ? 'Обновить операции' : 'Показать операции' }}</button></div>
        <p v-if="technicalError" role="alert" class="text-rose-300">{{ technicalError }} <button class="mx-link" @click="loadTechnical">Повторить</button></p>
        <p v-else-if="technicalLoading" role="status" class="mx-muted">Загружаем…</p>
        <div v-else-if="technical" class="grid md:grid-cols-2 gap-3 text-sm">
          <section class="mx-card p-3"><h3 class="font-medium">Сообщения · {{ technical.messages.inbound_total }} входящих</h3><div class="flex flex-wrap gap-2 mt-2"><span v-for="(count, state) in technical.messages.by_state" :key="state" class="mx-chip mx-chip-muted">{{ state }}: {{ count }}</span><span class="mx-chip mx-chip-warn">Внимание: {{ technical.messages.attention }}</span><span v-for="(count, purpose) in technical.messages.outbound_by_purpose" :key="purpose" class="mx-chip mx-chip-muted">Исходящие · {{ purpose }}: {{ count }}</span></div></section>
          <section class="mx-card p-3"><h3 class="font-medium">CRM · {{ technical.crm.logical_operations }} операций, {{ technical.crm.attempts_total }} попыток</h3><div class="flex flex-wrap gap-2 mt-2"><span v-for="(count, state) in technical.crm.operations_by_state" :key="state" class="mx-chip mx-chip-muted">{{ state }}: {{ count }}</span></div></section>
          <section class="mx-card p-3"><h3 class="font-medium">Текущие ошибки и неудачные попытки</h3><div class="flex flex-wrap gap-2 mt-2"><span v-for="(count, area) in technical.errors.current" :key="`current-${area}`" class="mx-chip mx-chip-err">{{ area }}: {{ count }}</span><span v-for="(count, area) in technical.errors.failed_attempts" :key="`failed-${area}`" class="mx-chip mx-chip-warn">{{ area }}: {{ count }} неудачных попыток</span><span v-if="!Object.keys(technical.errors.current).length && !Object.keys(technical.errors.failed_attempts).length" class="mx-muted">Ошибок нет.</span></div></section>
          <section class="mx-card p-3"><h3 class="font-medium">Каталог Bitrix24</h3><p v-if="technical.catalog" class="mx-muted mt-2">{{ technical.catalog.coverage === 'complete' ? 'Полное покрытие' : 'Покрытие неполное' }} · загружено {{ technical.catalog.imported_count }} · состояние {{ technical.catalog.state }}</p><p v-else class="mx-muted mt-2">Данные каталога недоступны.</p><p class="mx-muted text-xs mt-1">Состояние на {{ formatOverviewTime(technical.as_of) }} · {{ technical.timezone }} · {{ technical.coverage }}</p></section>
        </div>
        <div class="border-t pt-4" style="border-color:var(--mx-border)"><h2 class="font-semibold mb-3">Финансовые KPI и AI-аналитика</h2><AiAnalyticsPanel /></div>
      </div></details>
    </template>
  </main>
</template>
