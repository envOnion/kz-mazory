<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { RefreshCw } from 'lucide-vue-next'
import { post } from '../composables/api'
import { useOperationPage } from '../composables/useOperationPage'
import { routeId } from '../utils/routeId'
import { OPERATION_PERIODS, periodStart } from '../utils/operationFilters'
import { EmptyState, ErrorState, Skeleton, StatusBadge, formatTime, statusLabel } from '../shell/bits'
import type { QueueJob } from '../types/messenger'

// Очередь: логические работы стадий AI / delivery / history / CRM.
// Outbox-задание — одна логическая работа, не три разных.
const QUEUES = [
  { id: 'all', label: 'Все' }, { id: 'ai', label: 'Анализ (AI)' },
  { id: 'delivery', label: 'Отправки' }, { id: 'history', label: 'История' },
  { id: 'crm', label: 'CRM' },
]
const route = useRoute()
const router = useRouter()
const reason = ref(''), confirmedAbsent = ref(false), retryTarget = ref<QueueJob | null>(null)
const queue = ref(typeof route.query.queue === 'string' ? route.query.queue : 'all')
const state = ref(typeof route.query.state === 'string' ? route.query.state : '')
const team = ref(routeId(route.query.team)), source=ref(typeof route.query.source==='string'?route.query.source:'')
const period=ref(typeof route.query.period==='string'?route.query.period:'')
const retrying = ref<number | null>(null)

const STATE_OPTIONS = ['', 'pending', 'enqueued', 'processing', 'done', 'failed', 'unknown', 'error', 'stale']

function buildUrl(next: string | null) {
  const params = new URLSearchParams()
  if (queue.value !== 'all') params.set('queue', queue.value)
  if (state.value) params.set('state', state.value)
  if(team.value)params.set('team',String(team.value))
  if(source.value)params.set('source',source.value)
  const start=periodStart(period.value);if(start)params.set('from',start)
  if(next)params.set('cursor',next)
  return `/processing/jobs/?${params}`
}

const {rows,teams,error,loading,cursor,asOf,load}=useOperationPage<QueueJob>(buildUrl,()=>!retryTarget.value)
function applyFilters() {
  void router.replace({query:{...route.query,queue:queue.value==='all'?undefined:queue.value,state:state.value||undefined,
    team:team.value?String(team.value):undefined,source:source.value||undefined,period:period.value||undefined}})
}

async function retry(row: QueueJob) {
  retrying.value = row.id
  try {
    await post(`/outbox/${row.id}/retry/`, {reason:reason.value, provider_confirmed_not_delivered:confirmedAbsent.value})
    retryTarget.value = null; reason.value = ''; confirmedAbsent.value = false
    await load(true)
  } catch (e) { error.value = e instanceof Error ? e.message : 'Повтор не выполнен' }
  finally { retrying.value = null }
}

function queueTone(q: string) {
  return { ai: 'info', delivery: 'warn', crm: 'ok', history: 'muted' }[q] || 'muted'
}
const TYPE_LABELS: Record<string,string> = {source_work:'Обработка сообщения',notification:'Уведомление',otp:'Код входа',waha_control:'Управление WhatsApp',history_import:'Импорт истории',thread_backfill:'Распределение истории по темам',extract_message:'Анализ сообщения',route_message:'Определение темы',extract_thread:'Анализ темы',operation:'Фоновый запрос',attachment:'Обработка вложения',whatsapp_artifact:'Вложение WhatsApp',crm_sync:'Синхронизация CRM',autonomous_crm:'Запись в CRM',crm_catalog:'Каталог CRM',crm_import:'Импорт CRM',crm_match:'Поиск в CRM',decide_fact:'Принятие решения'}
const QUEUE_LABELS: Record<string, string> = { ai: 'AI', delivery: 'Отправки', history: 'История', crm: 'CRM' }

watch([()=>route.query.state,()=>route.query.queue,()=>route.query.team,()=>route.query.source,()=>route.query.period], () => {
  state.value=typeof route.query.state === 'string' ? route.query.state : ''
  queue.value=typeof route.query.queue === 'string' ? route.query.queue : 'all'
  team.value=routeId(route.query.team);source.value=typeof route.query.source==='string'?route.query.source:''
  period.value=typeof route.query.period==='string'?route.query.period:''
  void load(true)
})
</script>

<template>
  <section class="mx-panel-main flex flex-col" aria-label="Очередь">
    <header class="px-5 py-3 shrink-0" style="border-bottom:1px solid var(--mx-border)">
      <h1 style="font-size:23px;font-weight:600">Очередь</h1>
      <div class="flex gap-2 mt-2 flex-wrap items-center">
        <button v-for="q in QUEUES" :key="q.id" class="mx-chip" :class="queue === q.id ? 'mx-chip-info' : 'mx-chip-muted'" @click="queue = q.id; applyFilters()">{{ q.label }}</button>
        <select v-model="state" aria-label="Состояние задания" class="mx-select" style="width:auto" @change="applyFilters">
          <option v-for="option in STATE_OPTIONS" :key="option" :value="option">{{ option === '' ? 'Все состояния' : statusLabel(option) }}</option>
        </select>
        <select v-model="team" aria-label="Команда очереди" class="mx-select !w-auto" @change="applyFilters"><option :value="null">Все команды</option><option v-for="item in teams" :key="item.id" :value="item.id">{{ item.name }}</option></select>
        <select v-model="source" aria-label="Источник задания" class="mx-select !w-auto" @change="applyFilters"><option value="">Все источники</option><option value="source_work">Обработка сообщений</option><option value="outbox">Фоновые задания и отправки</option></select>
        <select v-model="period" aria-label="Период очереди" class="mx-select !w-auto" @change="applyFilters"><option v-for="item in OPERATION_PERIODS" :key="item.id" :value="item.id">{{ item.label }}</option></select>
        <span class="mx-chip mx-chip-muted">{{ asOf ? `Состояние на ${formatTime(asOf)}` : 'Загрузка состояния' }}</span>
      </div>
    </header>
    <div class="flex-1 overflow-y-auto p-4">
      <form v-if="retryTarget" class="mx-card p-4 mb-4 grid gap-3" @submit.prevent="retry(retryTarget)">
        <h2 class="font-semibold">Повтор задания #{{ retryTarget.id }}</h2>
        <label>Причина повтора<textarea v-model="reason" required maxlength="2000" class="mx-input" /></label>
        <label v-if="retryTarget.requires_delivery_confirmation"><input v-model="confirmedAbsent" type="checkbox" required /> Проверено у провайдера: сообщение не доставлено</label>
        <div class="flex gap-2"><button class="mx-btn mx-btn-primary" :disabled="retrying !== null">Подтвердить повтор</button><button class="mx-btn" type="button" @click="retryTarget = null">Отмена</button></div>
      </form>
      <ErrorState v-if="error" :text="`Данные не обновлены: ${error}`" :on-retry="() => load(true)" />
      <Skeleton v-if="loading && !rows.length" :rows="8" height="36px" />
      <EmptyState v-else-if="!rows.length && !error" title="Задач нет" text="В выбранных очередях сейчас нет работ." />
      <template v-else>
        <table class="mx-table">
          <thead><tr><th>Очередь</th><th>Тип</th><th>Состояние</th><th>Попытки</th><th>Следующий запуск</th><th>Ошибка</th><th>Обновлено</th><th></th></tr></thead>
          <tbody>
            <tr v-for="row in rows" :key="`${row.source}:${row.id}`" :data-job-source="row.source" :data-job-id="row.id">
              <td><span class="mx-chip" :class="`mx-chip-${queueTone(row.queue)}`">{{ QUEUE_LABELS[row.queue] || row.queue }}</span></td>
              <td>
                {{ TYPE_LABELS[row.type] || 'Служебная операция' }}
                <span v-if="row.raw_message_id" class="mx-muted"> · сообщение #{{ row.raw_message_id }}</span>
              </td>
              <td><StatusBadge :state="row.state" /></td>
              <td>{{ row.attempt_count ?? '—' }}</td>
              <td class="mx-muted">{{ row.next_attempt_at ? formatTime(row.next_attempt_at) : '—' }}</td>
              <td>{{ row.error_code || '—' }}</td>
              <td class="mx-muted">{{ formatTime(row.updated_at) }}</td>
              <td>
                <button
                  v-if="row.can_retry"
                  class="mx-btn" style="padding:4px 10px" :disabled="retrying === row.id"
                  title="Идемпотентный повтор через outbox" @click="retryTarget = row; reason = ''; confirmedAbsent = false"
                ><RefreshCw :size="12" /> Повторить</button>
                <RouterLink v-else-if="row.record_href" :to="row.record_href" class="mx-link">Открыть сообщение</RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
        <button v-if="cursor" class="mx-btn mt-4" :disabled="loading" @click="load(false)">Загрузить ещё</button>
      </template>
    </div>
  </section>
</template>
