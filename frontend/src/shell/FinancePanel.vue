<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { requestKey } from '../utils/requestKey'
import { api, post, pollOperation } from '../composables/api'
import { currentUser } from '../composables/session'
import type { Directory } from '../types/platform'
import type { FinanceData } from '../types/finance'
import { ErrorState, Skeleton } from './bits'
const data = ref<FinanceData | null>(null), directory = ref<Directory | null>(null)
const error = ref(''), notice = ref(''), busy = ref(false), period = ref('this_month')
const finance = computed(() => currentUser.value?.roles.some(role => role === 'finance' || role === 'admin'))
const lead = computed(() => currentUser.value?.roles.some(role => ['finance','team_lead','admin'].includes(role)))
const schedule = ref({project_id:null as number|null,due_date:'',amount:''})
const allocation = ref({payment_id:null as number|null,schedule_id:null as number|null,amount:''})
const target = ref({team_id:null as number|null,profile_id:null as number|null,month:'',amount:'',currency:'KZT'})
async function load() {
  error.value = ''
  try {
    const [summary, catalog] = await Promise.all([api<FinanceData>(`/finance/?period=${period.value}`), api<Directory>('/directory/')])
    data.value = summary; directory.value = catalog
  } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось загрузить финансы' }
}
async function command(fn:()=>Promise<unknown>) {
  busy.value = true; notice.value = ''; error.value = ''
  try { await fn(); await load(); notice.value = 'Изменения сохранены' }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось сохранить' }
  finally { busy.value = false }
}
async function saveTarget() {
  const old = data.value?.targets.find(row => row.team_id === target.value.team_id && row.profile_id === target.value.profile_id && row.month === target.value.month && row.currency === target.value.currency)
  await post('/finance/targets/', {...target.value, base_version:old?.version ?? 0})
}
async function exportPayments() {
  busy.value = true; error.value = ''
  try {
    const receipt = await post<{operation_id:number}>('/exports/', {period:period.value,idempotency_key:requestKey()})
    const result = await pollOperation<{filename:string;content:string}>(receipt.operation_id)
    const url = URL.createObjectURL(new Blob(['\uFEFF'+result.content],{type:'text/csv;charset=utf-8'}))
    const link = document.createElement('a'); link.href = url; link.download = result.filename; link.click(); URL.revokeObjectURL(url)
    notice.value = 'Экспорт сформирован и скачан'
  } catch (e) { error.value = e instanceof Error ? e.message : 'Экспорт не выполнен' }
  finally { busy.value = false }
}
const projectName = (id:number) => directory.value?.projects.find(p => p.id === id)?.name ?? `Проект #${id}`
onMounted(load)
</script>
<template>
  <div class="p-4 space-y-4">
    <div class="flex flex-wrap gap-2 items-center"><h2 class="text-lg font-semibold">Финансы и оплаты</h2><select v-model="period" class="mx-select !w-auto" aria-label="Период финансов" @change="load"><option value="this_month">Этот месяц</option><option value="last_month">Прошлый месяц</option><option value="quarter">Квартал</option><option value="year">Год</option></select><button class="mx-btn" :disabled="busy" @click="exportPayments">Экспорт оплат в CSV</button></div>
    <ErrorState v-if="error" :text="error" :on-retry="load" /><p v-if="notice" role="status" class="text-emerald-300">{{ notice }}</p><Skeleton v-if="!data && !error" />
    <template v-if="data">
      <div class="mx-card p-4"><p>Просрочено по графикам: {{ data.receivables.overdue }} {{ data.receivables.currency }}</p><p class="mx-muted">Проектов без подтверждённого графика: {{ data.receivables.unknown_schedule_projects }}. Отсутствие графика не означает отсутствие долга.</p></div>
      <div class="mx-card p-4 overflow-x-auto"><h3 class="font-semibold mb-2">Подтверждённые оплаты</h3><table class="mx-table"><thead><tr><th>Дата</th><th>Проект</th><th>Сумма</th></tr></thead><tbody><tr v-for="payment in data.payments" :key="payment.id"><td>{{ payment.payment_date }}</td><td>{{ projectName(payment.project_id) }}</td><td>{{ payment.amount }} {{ payment.currency }}</td></tr></tbody></table><p v-if="!data.payments.length" class="mx-muted">Оплат нет</p></div>
      <div class="mx-card p-4 overflow-x-auto"><h3 class="font-semibold mb-2">Графики оплат</h3><table class="mx-table"><thead><tr><th>Срок</th><th>Проект</th><th>Сумма</th></tr></thead><tbody><tr v-for="item in data.schedules" :key="item.id"><td>{{ item.due_date }}</td><td>{{ projectName(item.project_id) }}</td><td>{{ item.amount }} {{ item.currency }}</td></tr></tbody></table></div>
      <div v-if="finance" class="grid md:grid-cols-2 gap-3"><form class="mx-card p-4 space-y-2" @submit.prevent="command(() => post('/finance/schedules/',schedule))"><h3 class="font-semibold">Добавить срок оплаты</h3><label class="block">Проект<select aria-label="Проект" v-model="schedule.project_id" class="mx-select" required><option v-for="p in directory?.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label><label class="block">Срок<input v-model="schedule.due_date" type="date" class="mx-input" required /></label><label class="block">Сумма<input v-model="schedule.amount" inputmode="decimal" class="mx-input" required /></label><button class="mx-btn" :disabled="busy">Добавить срок</button></form>
      <form class="mx-card p-4 space-y-2" @submit.prevent="command(() => post('/finance/allocations/',allocation))"><h3 class="font-semibold">Распределить оплату</h3><label class="block">Оплата<select aria-label="Оплата" v-model="allocation.payment_id" class="mx-select" required><option v-for="p in data.payments" :key="p.id" :value="p.id">#{{ p.id }} · {{ projectName(p.project_id) }} · {{ p.amount }}</option></select></label><label class="block">Срок графика<select aria-label="Срок графика" v-model="allocation.schedule_id" class="mx-select" required><option v-for="s in data.schedules" :key="s.id" :value="s.id">#{{ s.id }} · {{ projectName(s.project_id) }} · {{ s.due_date }}</option></select></label><label class="block">Сумма<input v-model="allocation.amount" inputmode="decimal" class="mx-input" required /></label><button class="mx-btn" :disabled="busy">Распределить</button></form></div>
      <article class="mx-card p-4 overflow-x-auto"><h3 class="font-semibold mb-2">Планы продаж</h3><table class="mx-table"><thead><tr><th>Месяц</th><th>Сотрудник</th><th>План</th><th>Версия</th></tr></thead><tbody><tr v-for="item in data.targets" :key="item.id"><td>{{ item.month }}</td><td>{{ directory?.profiles.find(p => p.id === item.profile_id)?.full_name }}</td><td>{{ item.amount }} {{ item.currency }}</td><td>{{ item.version }}</td></tr></tbody></table></article>
      <form v-if="lead" class="mx-card p-4 grid sm:grid-cols-2 gap-3" @submit.prevent="command(saveTarget)"><h3 class="font-semibold sm:col-span-2">Утвердить новую версию плана</h3><label>Команда<select v-model="target.team_id" class="mx-select" required><option v-for="t in directory?.teams" :key="t.id" :value="t.id">{{ t.name }}</option></select></label><label>Сотрудник<select v-model="target.profile_id" class="mx-select" required><option v-for="p in directory?.profiles" :key="p.id" :value="p.id">{{ p.full_name }}</option></select></label><label>Первый день месяца<input v-model="target.month" type="date" class="mx-input" required /></label><label>План<input v-model="target.amount" class="mx-input" inputmode="decimal" required /></label><label>Валюта<select v-model="target.currency" class="mx-select"><option v-for="c in ['KZT','USD','EUR','RUB']" :key="c">{{ c }}</option></select></label><button class="mx-btn self-end justify-self-start" :disabled="busy">Утвердить план</button></form>
    </template>
  </div>
</template>
