<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import FinancePanel from '../shell/FinancePanel.vue'
import AttachmentPanel from '../shell/AttachmentPanel.vue'
import WorkspaceView from '../components/WorkspaceView.vue'
import { RefreshCw } from 'lucide-vue-next'
import { api } from '../composables/api'
import { EmptyState, ErrorState, Skeleton, formatTime } from '../shell/bits'
import AiAnalyticsPanel from '../shell/AiAnalyticsPanel.vue'
import type { AnalyticsOperations } from '../types/messenger'

const route = useRoute()
const views = [{id:'operations',label:'Обзор'}, {id:'projects',label:'Проекты'}, {id:'finance',label:'Финансы'}, {id:'documents',label:'Вложения'}, {id:'review',label:'Факты и решения'}, {id:'tasks',label:'Обязательства'}, {id:'reports',label:'Отчёты'}, {id:'notifications',label:'Уведомления'}]
const view = computed(() => typeof route.query.view === 'string' && views.some(v => v.id === route.query.view) ? route.query.view : 'operations')

// Аналитика: операционные метрики с определениями + сохранённый AI-чат KPI.
const data = ref<AnalyticsOperations | null>(null)
const error = ref('')
const loading = ref(true)

const STATE_LABELS: Record<string, string> = {
  received: 'Получено', queued: 'В очереди', analyzing: 'Анализируется', need_context: 'Нужен контекст',
  analyzed: 'Проанализировано', no_crm_actions: 'Без CRM-действий', error: 'Ошибка', pending: 'Ожидает',
  processing: 'Выполняется', done: 'Выполнено', completed: 'Завершено', failed: 'Ошибка',
  stale: 'Зависло', sending: 'Отправляется', accepted: 'Запрос принят', confirmed: 'Подтверждено',
  unknown: 'Результат неизвестен', unavailable: 'Результат недоступен', reading: 'Проверяется чтением',
  clarification: 'Уточнение', notification: 'Уведомление', otp: 'Код входа', source: 'Сообщение',
}

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await api<AnalyticsOperations>('/analytics/operations/') }
  catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка сети' }
  finally { loading.value = false }
}
onMounted(load)

const coverageText = (value: string) => value === 'scoped' ? 'в границах вашего доступа' : value
</script>

<template>
  <section class="mx-panel-main overflow-y-auto" aria-label="Аналитика">
    <header class="px-5 py-3 sticky top-0 z-10 shrink-0" style="border-bottom:1px solid var(--mx-border);background:var(--mx-main)">
      <h1 style="font-size:23px;font-weight:600">Аналитика</h1>
      <nav class="flex gap-1 flex-wrap mt-2" aria-label="Данные и аналитика"><RouterLink v-for="item in views" :key="item.id" :to="{path: '/analytics', query: {view:item.id}}" class="mx-tab" :class="view === item.id ? 'mx-tab-active' : ''">{{ item.label }}</RouterLink></nav>
      <div v-if="data" class="mx-muted text-[11px] mt-1">
        Состояние на {{ formatTime(data.as_of) }} · часовой пояс {{ data.timezone }} · выборка {{ coverageText(data.coverage) }}
      </div>
    </header>
    <FinancePanel v-if="view === 'finance'" /><AttachmentPanel v-else-if="view === 'documents'" />
    <WorkspaceView v-else-if="view !== 'operations'" :initial-tab="view" />
    <div v-else class="p-5 flex flex-col gap-5 max-w-6xl">
      <Skeleton v-if="loading" :rows="8" height="72px" />
      <ErrorState v-else-if="error" :text="error" :on-retry="load" />
      <EmptyState v-else-if="!data" title="Нет данных" text="Метрики появятся, когда в системе появятся сообщения." />
      <template v-else>
        <div>
          <h2 class="font-semibold mb-2">Сообщения <span class="mx-muted text-[11px]font-normal">· уникальные входящие WhatsApp по времени источника; состояние этой когорты на текущий момент</span></h2>
          <div class="grid gap-2" style="grid-template-columns:repeat(auto-fill,minmax(170px,1fr))">
            <RouterLink to="/messenger?cohort=source" class="mx-card p-3 hover:brightness-125">
              <div class="mx-muted text-[11px]">Всего входящих</div>
              <div style="font-size:22px;font-weight:600">{{ data.messages.inbound_total }}</div>
            </RouterLink>
            <RouterLink v-for="(count, state) in data.messages.by_state" :key="state" :to="`/messenger?cohort=source&analysis=${state}`" class="mx-card p-3 hover:brightness-125">
              <div class="mx-muted text-[11px]">{{ STATE_LABELS[state] || state }}</div>
              <div style="font-size:22px;font-weight:600">{{ count }}</div>
            </RouterLink>
            <RouterLink to="/messenger?cohort=source&attention=1" class="mx-card p-3 hover:brightness-125" style="border-color:rgba(255,190,115,.4)">
              <div class="mx-muted text-[11px]">Требуют внимания</div>
              <div style="font-size:22px;font-weight:600;color:var(--mx-warn)">{{ data.messages.attention }}</div>
            </RouterLink>
          </div>
          <div class="flex gap-2 mt-2 flex-wrap">
            <span v-for="(count, purpose) in data.messages.outbound_by_purpose" :key="purpose" class="mx-chip mx-chip-muted">
              {{ STATE_LABELS[purpose] || purpose }} (исходящие): {{ count }}
            </span>
            <span v-if="!Object.keys(data.messages.outbound_by_purpose).length" class="mx-chip mx-chip-muted">Исходящих не было</span>
          </div>
        </div>

        <div>
          <h2 class="font-semibold mb-2">Операции CRM <span class="mx-muted text-[11px]font-normal">· по уникальному логическому действию; попытки показаны отдельно</span></h2>
          <div class="flex gap-2 flex-wrap">
            <RouterLink :to="`/crm-changes?state=${state}`" v-for="(count, state) in data.crm.operations_by_state" :key="state" class="mx-chip hover:brightness-125" :class="state === 'confirmed' ? 'mx-chip-ok' : (['failed','unknown','unavailable'].includes(state) ? 'mx-chip-err' : 'mx-chip-muted')">
              {{ STATE_LABELS[state] || state }}: {{ count }}
            </RouterLink>
            <span class="mx-chip mx-chip-muted">Логических операций: {{ data.crm.logical_operations }}</span>
            <span class="mx-chip mx-chip-muted">Попыток всего: {{ data.crm.attempts_total }}</span>
          </div>
        </div>

        <div>
          <h2 class="font-semibold mb-2">Ошибки <span class="mx-muted text-[11px]font-normal">· «текущие проблемы» и «все неудачные попытки» — разные ряды</span></h2>
          <div class="grid gap-2" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">
            <RouterLink v-for="(count, area) in data.errors.current" :key="`c-${area}`" to="/queue?state=error" class="mx-card p-3 hover:brightness-125">
              <div class="mx-muted text-[11px]">Текущие · {{ STATE_LABELS[area] || area }}</div>
              <div style="font-size:20px;font-weight:600" :style="count ? 'color:var(--mx-err)' : ''">{{ count }}</div>
            </RouterLink>
            <RouterLink v-for="(count, area) in data.errors.failed_attempts" :key="`f-${area}`" to="/queue" class="mx-card p-3 hover:brightness-125">
              <div class="mx-muted text-[11px]">Неудачные попытки · {{ STATE_LABELS[area] || area }}</div>
              <div style="font-size:20px;font-weight:600">{{ count }}</div>
            </RouterLink>
            <div v-if="!Object.keys(data.errors.current).length && !Object.keys(data.errors.failed_attempts).length" class="mx-card p-3 mx-muted">Ошибок нет.</div>
          </div>
        </div>

        <div>
          <h2 class="font-semibold mb-2">Динамика сообщений <span class="mx-muted text-[11px]font-normal">· по дням времени источника</span></h2>
          <div class="mx-card p-3">
            <div v-if="!data.dynamics.length" class="mx-muted">Нет сообщений за период.</div>
            <table v-else class="mx-table">
              <tbody>
                <tr v-for="row in data.dynamics" :key="row.day ?? 'unknown'">
                  <td style="width:120px" class="mx-muted">{{ row.day ?? 'Время неизвестно' }}</td>
                  <td class="w-full">
                    <div class="h-3 rounded" :style="{ width: `${Math.max(2, Math.min(100, row.messages))}%`, background: 'var(--mx-accent)', maxWidth: '100%' }" />
                  </td>
                  <td class="text-right" style="width:60px">{{ row.messages }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="data.catalog">
          <h2 class="font-semibold mb-2">Каталог Bitrix24 <span class="mx-muted text-[11px]font-normal">· размер локальной выборки не равен «всего в Bitrix»</span></h2>
          <div class="mx-card p-3 flex gap-3 flex-wrap items-center">
            <span class="mx-chip" :class="data.catalog.coverage === 'complete' ? 'mx-chip-ok' : 'mx-chip-warn'">
              {{ data.catalog.coverage === 'complete' ? 'Каталог синхронизирован полностью' : 'Выборка неполная' }}
            </span>
            <span class="mx-chip mx-chip-muted">Загружено объектов: {{ data.catalog.imported_count }}</span>
            <span class="mx-chip mx-chip-muted">Последняя синхронизация: {{ formatTime(data.catalog.last_success_at) }}</span>
            <RouterLink to="/settings" class="mx-link text-[12px]">Синхронизация — в Настройках →</RouterLink>
          </div>
        </div>

        <div class="flex justify-end">
          <button class="mx-btn" @click="load"><RefreshCw :size="13" /> Обновить</button>
        </div>

        <div class="mx-card p-4">
          <h2 class="font-semibold mb-3">Финансовые KPI и AI-аналитика <span class="mx-muted text-[11px]font-normal">· существующие диалоги и артефакты сохранены</span></h2>
          <AiAnalyticsPanel />
        </div>
      </template>
    </div>
  </section>
</template>
