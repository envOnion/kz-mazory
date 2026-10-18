<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ExternalLink } from 'lucide-vue-next'
import { useOperationPage } from '../composables/useOperationPage'
import { routeId } from '../utils/routeId'
import { OPERATION_PERIODS, periodStart } from '../utils/operationFilters'
import { EmptyState, ErrorState, Skeleton, StatusBadge, formatTime } from '../shell/bits'
import type { CrmChangeRow } from '../types/messenger'

// Изменения CRM: журнал подтверждений с «было → стало», ссылкой Bitrix и стадией.
const OBJECT_TYPES = [
  { id: '', label: 'Все объекты' }, { id: 'deal', label: 'Сделки' },
  { id: 'task', label: 'Задачи' }, { id: 'company', label: 'Контрагенты (компании)' },
  { id: 'contact', label: 'Контакты' },
]
const route = useRoute()
const router = useRouter()
const objectType = ref(typeof route.query.object_type === 'string' ? route.query.object_type : '')
const state = ref(typeof route.query.state === 'string' ? route.query.state : '')
const period = ref(typeof route.query.period === 'string' ? route.query.period : '')
const team=ref(routeId(route.query.team)),source=ref(typeof route.query.source==='string'?route.query.source:'')
const action=ref(typeof route.query.action==='string'?route.query.action:'')
const externalId=ref(typeof route.query.external_id==='string'?route.query.external_id:'')

function buildUrl(next: string | null) {
  const params = new URLSearchParams()
  if (typeof route.query.receipt === 'string') params.set('receipt_id', route.query.receipt)
  if (objectType.value) params.set('object_type', objectType.value)
  if (state.value) params.set('state', state.value)
  if(team.value)params.set('team',String(team.value))
  if(source.value)params.set('source',source.value)
  if(action.value)params.set('action',action.value)
  if(externalId.value.trim())params.set('external_id',externalId.value.trim())
  const from = periodStart(period.value)
  if (from) params.set('from', from)
  if(next)params.set('cursor',next)
  return `/crm/changes/?${params}`
}

const {rows,teams,stateLabels,error,loading,cursor,asOf,load}=useOperationPage<CrmChangeRow>(buildUrl)
function applyFilters() {
  void router.replace({query:{...route.query,object_type:objectType.value||undefined,state:state.value||undefined,period:period.value||undefined,
    team:team.value?String(team.value):undefined,source:source.value||undefined,action:action.value||undefined,external_id:externalId.value.trim()||undefined}})
}

function diffText(value: unknown): string {
  if (value === null || value === undefined) return 'нет данных'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    if (!entries.length) return 'нет данных'
    return entries.map(([key, item]) => `${key}: ${item === null || item === undefined ? '—' : String(item)}`).join('; ')
  }
  return String(value)
}
const ACTION_LABELS: Record<string, string> = {
  create:'Создание',add:'Создание',update:'Изменение',change:'Изменение',delete:'Удаление',complete:'Завершение',defer:'Отложено',restored:'Восстановленная доставка',
  project:'Изменение проекта', payment:'Изменение оплаты', commitment:'Изменение обязательства', company:'Изменение контрагента',
  deal_created: 'Новая сделка', deal_updated: 'Обновлена сделка',
  company_created: 'Новый контрагент', task_created: 'Новая задача',
  task_updated: 'Обновлена задача', task_completed: 'Задача завершена',
}
const OBJECT_LABELS:Record<string,string>={deal:'Сделка',task:'Задача',company:'Компания',contact:'Контакт',timeline:'Комментарий'}
const objectLabel=(kind:string|null)=>OBJECT_LABELS[kind ?? ''] || 'Объект CRM'

watch([()=>route.query.receipt,()=>route.query.object_type,()=>route.query.state,()=>route.query.period,()=>route.query.team,()=>route.query.source,()=>route.query.action,()=>route.query.external_id], () => {
  objectType.value=typeof route.query.object_type === 'string' ? route.query.object_type : ''
  state.value=typeof route.query.state === 'string' ? route.query.state : ''
  period.value=typeof route.query.period === 'string' ? route.query.period : ''
  team.value=routeId(route.query.team);source.value=typeof route.query.source==='string'?route.query.source:''
  action.value=typeof route.query.action==='string'?route.query.action:''
  externalId.value=typeof route.query.external_id==='string'?route.query.external_id:''
  void load(true)
})
</script>

<template>
  <section class="mx-panel-main flex flex-col" aria-label="Изменения CRM">
    <header class="px-5 py-3 shrink-0" style="border-bottom:1px solid var(--mx-border)">
      <h1 style="font-size:23px;font-weight:600">Изменения CRM</h1><RouterLink v-if="route.query.receipt" to="/crm-changes" class="mx-link">Все изменения →</RouterLink>
      <div class="flex gap-2 mt-2 flex-wrap">
        <select v-model="objectType" aria-label="Тип объекта CRM" class="mx-select" style="width:auto" @change="applyFilters">
          <option v-for="t in OBJECT_TYPES" :key="t.id" :value="t.id">{{ t.label }}</option>
        </select>
        <select v-model="state" aria-label="Результат CRM" class="mx-select" style="width:auto" @change="applyFilters">
          <option value="">Все результаты</option>
          <option v-for="(label, key) in stateLabels" :key="key" :value="key">{{ label }}</option>
        </select>
        <select v-model="period" aria-label="Период CRM" class="mx-select" style="width:auto" @change="applyFilters">
          <option v-for="p in OPERATION_PERIODS" :key="p.id" :value="p.id">{{ p.label }}</option>
        </select>
        <select v-model="team" aria-label="Команда CRM" class="mx-select !w-auto" @change="applyFilters"><option :value="null">Все команды</option><option v-for="item in teams" :key="item.id" :value="item.id">{{ item.name }}</option></select>
        <select v-model="source" aria-label="Источник CRM" class="mx-select !w-auto" @change="applyFilters"><option value="">Все источники</option><option value="delivery">Доставки изменений</option><option value="journal">Журнал вызовов</option></select>
        <select v-model="action" aria-label="Действие CRM" class="mx-select !w-auto" @change="applyFilters"><option value="">Все действия</option><option value="add">Создание</option><option value="update">Изменение</option><option value="complete">Завершение</option><option value="delete">Удаление</option><option value="defer">Отложено</option></select>
        <form class="flex gap-2 min-w-0 w-full sm:w-auto" @submit.prevent="applyFilters"><input v-model="externalId" aria-label="Внешний ID CRM" maxlength="64" class="mx-input min-w-0 flex-1" placeholder="ID объекта в Bitrix24"/><button class="mx-btn shrink-0">Найти объект</button></form>
        <span class="mx-muted text-xs self-center">{{ asOf ? `Состояние на ${formatTime(asOf)}` : 'Загрузка состояния' }}</span>
      </div>
    </header>
    <div class="flex-1 overflow-y-auto p-4">
      <ErrorState v-if="error" :text="`Данные не обновлены: ${error}`" :on-retry="() => load(true)" />
      <Skeleton v-if="loading && !rows.length" :rows="7" height="64px" />
      <EmptyState v-else-if="!rows.length && !error" title="Изменений нет" text="По выбранным фильтрам операций с CRM не найдено." />
      <template v-else>
        <div v-for="row in rows" :key="`${row.source}:${row.id}`" :data-crm-id="`${row.source}:${row.id}`" class="mx-card p-3 mb-2">
          <div class="flex items-center gap-2 flex-wrap">
            <span v-if="row.method" class="mx-chip">{{ row.method }}</span>
            <details v-if="row.methods && row.methods.length > 1" class="text-xs mx-muted"><summary>Вызовы этой операции</summary><p v-for="method in row.methods" :key="method">{{ method }}</p></details>
            <span class="font-medium">{{ row.action ? (ACTION_LABELS[row.action] || row.action) : (row.object_type ? `Изменение · ${row.object_type}` : 'Операция CRM') }}</span>
            <StatusBadge :state="row.state" :extra="stateLabels[row.state]" />
            <span class="mx-muted text-xs">{{ row.actor_label }}</span>
            <span v-if="row.external_id" class="mx-chip mx-chip-muted">{{ objectLabel(row.object_type) }} #{{ row.external_id }}</span>
            <span class="mx-muted ml-auto text-[11px]">{{ formatTime(row.updated_at) }}</span>
          </div>
          <div v-if="row.before !== undefined || row.after !== undefined" class="text-[12px] mt-1.5 grid gap-0.5">
            <span class="mx-muted">Было: {{ diffText(row.before) }}</span>
            <span>{{ row.state === 'confirmed' ? 'Стало' : 'Запрошено' }}: {{ diffText(row.after) }}</span>
          </div>
          <div class="flex gap-3 items-center mt-1.5 text-[12px]">
            <a v-if="row.external_url" class="mx-link inline-flex items-center gap-1" :href="row.external_url" target="_blank" rel="noopener noreferrer">Открыть в Bitrix24 <ExternalLink :size="12" /></a>
            <span v-else-if="row.external_id" class="mx-muted">Внешний ID известен; ссылка не подтверждена</span>
            <span v-else-if="row.local_id" class="mx-muted">Локальный объект #{{ row.local_id }} — внешний ID ещё не получен</span>
            <RouterLink v-if="row.record_href" :to="row.record_href" class="mx-link">Исходное сообщение →</RouterLink>
            <span v-if="row.reconstructed" class="mx-chip">Восстановленная доставка</span>
            <span v-if="row.error_code" class="mx-chip mx-chip-err">✕ {{ row.error_code }}</span>
          </div>
        </div>
        <button v-if="cursor" class="mx-btn" :disabled="loading" @click="load(false)">Загрузить ещё</button>
      </template>
    </div>
  </section>
</template>
