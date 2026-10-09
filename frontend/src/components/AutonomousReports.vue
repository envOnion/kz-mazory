<template>
  <section class="space-y-4" aria-label="Автоматические отчеты по WhatsApp">
    <p v-if="error" role="alert" class="panel text-rose-300">{{ error }}</p>
    <p v-if="!data && !error">Загрузка отчета…</p>
    <template v-if="data">
      <header class="panel space-y-2">
        <h2 class="text-xl font-semibold">Учет по WhatsApp</h2>
        <div class="space-y-1" role="region" aria-label="Режимы обработки и интеграции">
          <p>{{ data.enabled ? 'Автоматическая обработка WhatsApp включена' : 'Автоматическая обработка WhatsApp выключена' }}</p>
          <p>{{ data.crm_status.integration_enabled ? 'Интеграция Bitrix включена' : 'Интеграция Bitrix выключена' }}</p>
          <p>{{ data.crm_status.effective_autonomous_write_enabled ? 'Автоматическая запись из WhatsApp в CRM включена' : 'Автоматическая запись из WhatsApp в CRM выключена' }}</p>
          <p v-if="data.crm_status.disabled_reason" class="text-sm text-slate-400">{{ crmDisabledReason(data.crm_status.disabled_reason) }}</p>
        </div>
        <p class="text-sm text-slate-400">Сведения на {{ date(data.as_of) }}. Здесь показаны зарегистрированные сообщения о движениях; полнота доступной переписки указана ниже.</p>
        <button class="btn" :disabled="loading" @click="load">Обновить</button>
      </header>
      <div class="grid md:grid-cols-2 gap-3">
        <article v-for="item in data.totals" :key="`${item.currency}:${item.direction}:${item.amount_precision}`" class="panel">
          <p>{{ item.direction === 'income' ? 'Поступления' : 'Расходы' }} · {{ item.currency }} · {{ item.amount_precision === 'exact' ? 'Точные суммы' : 'Приблизительные суммы' }}</p>
          <strong class="text-xl">{{ amount(item.amount) }}</strong><p class="text-sm text-slate-400">Событий: {{ item.count }}</p>
        </article>
      </div>
      <p v-if="!data.totals.length" class="panel">Зарегистрированных финансовых движений нет. Это не означает, что движений не было.</p>
      <article v-for="currency in currencies" :key="currency" class="panel space-y-3">
        <h3 class="font-semibold">Движения за 30 дней · {{ currency }}</h3>
        <div v-for="(point, index) in data.series.filter(p => p.currency === currency)" :key="index" class="space-y-1">
          <p class="text-sm">{{ point.payment_date }} · {{ point.direction === 'income' ? 'Поступило' : 'Расход' }} · {{ point.amount_precision === 'exact' ? '' : 'Приблизительно ' }}{{ amount(point.amount) }}</p>
          <div class="rounded h-2" :class="point.direction === 'income' ? 'bg-emerald-400' : 'bg-amber-400'" :style="{ width: `${width(point.amount, currency)}%` }" />
        </div>
      </article>
      <article class="panel space-y-2">
        <h3 class="font-semibold">График ожидаемых движений</h3>
        <p v-for="item in data.schedule" :key="item.id">{{ item.due_date }} · {{ item.project__name }} · {{ item.direction === 'income' ? 'Ожидаем поступление' : 'Планируем расход' }} · {{ amount(item.amount) }} {{ item.currency }}</p>
        <p v-if="!data.schedule.length" class="text-slate-400">График с известными суммами и датами пока отсутствует.</p>
      </article>
      <article class="panel space-y-2">
        <h3 class="font-semibold">Сообщенные показатели и планы</h3>
        <details v-for="item in data.observations || []" :key="item.id"><summary>{{ observationLabel(item.event_type) }} · {{ item.project__name || 'Объект не определен' }} · {{ item.payload.amount ? amount(item.payload.amount) : 'Сумма не указана' }} {{ item.payload.currency }}</summary><p class="text-sm whitespace-pre-wrap">{{ item.payload.evidence }}</p><p>Эти показатели не увеличивают сумму поступлений.</p></details>
      </article>
      <article class="panel space-y-2">
        <h3 class="font-semibold">Доставка в CRM</h3>
        <p v-for="item in data.crm_deliveries || []" :key="item.id">Запись #{{ item.id }} · {{ item.state }} · {{ deliveryReason(item.error_code) }}</p>
        <p v-if="!data.crm_deliveries?.length">Незавершенных доставок в доступной выборке нет.</p>
      </article>
      <article class="panel space-y-2">
        <h3 class="font-semibold">Ожидает данных</h3>
        <p v-for="item in data.waiting" :key="item.candidate_id">Факт #{{ item.candidate_id }}: {{ item.explanation }}</p>
        <p v-if="!data.waiting.length" class="text-slate-400">Отложенных решений в доступной выборке нет.</p>
      </article>
      <article class="panel space-y-2">
        <h3 class="font-semibold">Покрытие источников</h3>
        <p v-if="!data.coverage.length">Покрытие пока не рассчитано или недоступно для вашей роли.</p>
        <div v-for="item in data.coverage" :key="item.source_scope">
          <p>Непрерывно обработанная полученная история: {{ item.complete_through ? date(item.complete_through) : 'есть пробелы в начале истории' }}.</p>
          <p class="text-sm text-slate-400">Пробелов в показанном списке: {{ item.gaps.length }}. Обработка всей полученной истории не доказывает доступность всей переписки бизнеса.</p>
        </div>
      </article>
      <article class="panel space-y-2"><h3 class="font-semibold">Сохраненные отчеты</h3><details v-for="report in data.reports" :key="report.id"><summary>Отчет #{{ report.id }} · {{ date(report.created_at) }}</summary><p>Проектов: {{ report.payload.projects }} · Договор известен: {{ report.payload.contract_known }} · Открытых обязательств: {{ report.payload.open_commitments }} · Без срока: {{ report.payload.missing_deadline }}</p><p v-for="item in report.payload.totals" :key="`${item.currency}:${item.direction}:${item.amount_precision}`">{{ item.direction === 'income' ? 'Поступления' : 'Расходы' }} · {{ item.amount_precision === 'exact' ? 'Точно' : 'Приблизительно' }} {{ amount(item.amount) }} {{ item.currency }}</p></details><p v-if="!data.reports.length">Автоматических отчетов пока нет.</p></article>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../composables/api'
import type { AutonomousOverview, CrmDisabledReason } from '../types/autonomous'
const data = ref<AutonomousOverview | null>(null), error = ref(''), loading = ref(false)
const currencies = computed(() => [...new Set(data.value?.series.map(item => item.currency) || [])])
const date = (value: string) => new Date(value).toLocaleString('ru-RU')
const amount = (value: string) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value))
function crmDisabledReason(reason: CrmDisabledReason): string {
  const labels: Record<CrmDisabledReason, string> = {
    integration_disabled: 'Интеграция Bitrix выключена.',
    webhook_missing: 'Не задан Webhook для подключения к Bitrix.',
    autonomous_crm_disabled: 'Автоматическая запись результатов WhatsApp в CRM выключена в настройках ИИ.',
  }
  return labels[reason]
}
function width(value: string, currency: string) {
  const max = Math.max(1, ...(data.value?.series.filter(item => item.currency === currency).map(item => Math.abs(Number(item.amount))) || []))
  return Math.max(1, Math.abs(Number(value)) / max * 100)
}
function observationLabel(kind: string) { return ({ reported_cumulative: 'Накопленный итог', reported_balance: 'Остаток', reported_debt: 'Долг', reported_invoice: 'Счет', reported_transfer: 'Перевод', payment_schedule: 'План движения' } as Record<string, string>)[kind] || 'Финансовый показатель' }
function deliveryReason(code: string) { return ({ blocked_missing_external_assignee: 'Исполнитель известен по переписке, но его аккаунт CRM не установлен', crm_deal_delivery_pending: 'Ожидается связь со сделкой CRM', crm_company_identity_unresolved: 'Компания с таким названием уже есть, идентичность не установлена', crm_rejected: 'CRM отклонила запись', crm_disabled: 'Подключение CRM выключено', autonomous_crm_paused: 'Запись в CRM приостановлена' } as Record<string, string>)[code] || (code ? 'Доставка задержана; причина сохранена в журнале системы' : 'Ожидается обработка') }
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await api<AutonomousOverview>('/autonomous/overview/') }
  catch (exc) { error.value = exc instanceof Error ? exc.message : 'Не удалось загрузить отчет' }
  finally { loading.value = false }
}
onMounted(load)
</script>
