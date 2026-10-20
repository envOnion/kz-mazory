<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { requestKey } from '../utils/requestKey'
import { routeId } from '../utils/routeId'
import { api, post } from '../composables/api'
import { currentUser } from '../composables/session'
import AccessGrants from '../shell/AccessGrants.vue'
import SettingsForm from '../shell/SettingsForm.vue'
import ConnectionSecretForm from '../shell/ConnectionSecretForm.vue'
import AiModelSettings from '../components/settings/AiModelSettings.vue'
import DiagnosticsPanel from '../shell/DiagnosticsPanel.vue'
import WorkspaceView from '../components/WorkspaceView.vue'
import { ErrorState, Skeleton, EmptyState, StatusBadge, formatTime } from '../shell/bits'
import type { SettingsAccess, SettingsPage, SettingsRow, HistoryPage } from '../types/settings'
const route = useRoute(), router = useRouter()
const access = ref<SettingsAccess | null>(null), data = ref<SettingsPage | null>(null)
const history = ref<HistoryPage | null>(null), selectedConfig = ref<number | null>(null)
const selectedAiConfig = ref<number | null>(null), newAiConfigName = ref('')
const error = ref(''), notice = ref(''), loading = ref(true), busy = ref(false), code = ref(''), qr = ref('')
const creating = ref(false)
const active = computed(() => typeof route.query.section === 'string' ? route.query.section : 'whatsapp')
const sections = computed(() => [
  ...(access.value?.can_integrate ? [{id: 'whatsapp', label: 'WhatsApp'}] : []),
  ...(access.value?.can_integrate && access.value.can_ai ? [{id:'ai',label:'ИИ'}] : []),
  ...(access.value?.can_integrate && access.value.can_bitrix ? [{id:'bitrix',label:'Bitrix24'}] : []),
  ...((currentUser.value?.roles.includes('team_lead') || currentUser.value?.roles.includes('finance')) ? [{id: 'people', label: 'Участники'}] : []),
  ...(access.value?.can_administer_access ? [{id: 'access', label: 'Доступ и команды'}, {id: 'system', label: 'Система и аудит'}] : []),
])
const memberData = ref<{memberships: {id: number; user__username: string; team_id: number; role: string; status: string}[]; teams: {id:number; name:string}[]; clients: {id:number; user_id:number; project_id:number; status:string}[]} | null>(null)
const invite = ref({phone: '', name: '', team_id: null as number | null, role: 'manager', status: 'invited'})
const upload = ref<File | null>(null), exportZone = ref('Asia/Almaty'), dateOrder = ref('DMY')
let timer: number | null = null
let generation = 0
function select(section: string) { void router.push({path: '/settings', query: {section}}) }
async function load() {
  const version = ++generation
  loading.value = true; error.value = ''; creating.value = false; data.value = null; history.value = null; qr.value = ''
  try {
    const permissions = await api<SettingsAccess>('/settings/access/')
    if (version !== generation) return
    access.value = permissions
    if (permissions.mfa_required) return
    if (!sections.value.some(s => s.id === active.value)) {
      if (sections.value[0]) select(sections.value[0].id)
      return
    }
    if (['ai','whatsapp','bitrix'].includes(active.value)) {
      const result = await api<SettingsPage>(`/settings/${active.value}/`)
      if (version === generation) {
        data.value = result
        if (active.value === 'ai' && !result.items.some(row => row.id === selectedAiConfig.value)) {
          selectedAiConfig.value = result.items.find(row => row.is_primary)?.id ?? result.items.find(row => row.is_active)?.id ?? result.items[0]?.id ?? null
        }
        const configId = routeId(route.query.config); if (active.value === 'whatsapp' && configId) await openHistory(configId)
      }
    } else if (active.value === 'access') {
      const result = await api<NonNullable<typeof memberData.value>>('/access/')
      if (version === generation) { memberData.value = result; invite.value.team_id = result.teams[0]?.id ?? null }
    }
  } catch (e) { if (version === generation) error.value = e instanceof Error ? e.message : 'Ошибка сети' }
  finally { if (version === generation) loading.value = false }
}
async function challenge() {
  busy.value = true; error.value = ''
  try { await post('/settings/access/', {code: code.value}); code.value = ''; await load() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось подтвердить MFA' }
  finally { busy.value = false }
}
async function action(fn: () => Promise<unknown>, text: string) {
  busy.value = true; notice.value = ''; error.value = ''
  try { await fn(); notice.value = text }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось выполнить действие' }
  finally { busy.value = false }
}
async function createAiConfiguration() {
  const name = newAiConfigName.value.trim()
  if (!name) { error.value = 'Укажите название конфигурации.'; return }
  await action(async () => {
    const created = await post<{ id: number }>('/settings/ai/', { name })
    const refreshed = await api<SettingsPage>('/settings/ai/')
    data.value = refreshed
    selectedAiConfig.value = refreshed.items.some(row => row.id === created.id) ? created.id : refreshed.items[0]?.id ?? null
    newAiConfigName.value = ''
    creating.value = false
  }, 'Конфигурация создана. Включите её после настройки подключения.')
}
async function updateAiConfiguration(row: SettingsRow, changes: Record<string, boolean>) {
  await action(async () => {
    await api(`/settings/ai/${row.id}/`, { method: 'PATCH', body: JSON.stringify(changes) })
    data.value = await api<SettingsPage>('/settings/ai/')
  }, 'Состояние конфигураций обновлено.')
}
function setAiActive(row: SettingsRow, activeState: boolean) {
  if (!activeState && row.is_primary && data.value?.items.some(other => other.id !== row.id && other.is_active)) {
    error.value = 'Сначала выберите другую основную конфигурацию.'
    return
  }
  void updateAiConfiguration(row, { is_active: activeState })
}
function setAiPrimary(row: SettingsRow) {
  if (!row.is_active) void updateAiConfiguration(row, { is_active: true, is_primary: true })
  else void updateAiConfiguration(row, { is_primary: true })
}
function safeProviderHost(value: string): string {
  try { return new URL(value).host } catch { return '' }
}
async function control(row: SettingsRow, command: string) {
  await action(() => post(`/settings/whatsapp/${row.id}/`, {action: command}), 'Команда поставлена в очередь. Результат появится после выполнения.')
}
async function refreshStatus() {
  if (document.hidden || active.value !== 'whatsapp' || busy.value || access.value?.mfa_required) return
  const version=generation, config=selectedConfig.value
  try {
    const result = await api<SettingsPage>('/settings/whatsapp/')
    if(version !== generation || active.value !== 'whatsapp') return
    // Do not replace editable forms while the user is typing.
    if (data.value) for (const row of data.value.items) {
      const updated = result.items.find(item => item.id === row.id)
      if (updated) { row.status = updated.status; row.control = updated.control; row.qr_image = updated.qr_image }
    }
    if (config) {
      const updatedHistory = await api<HistoryPage>(`/settings/whatsapp/${config}/history/`)
      if (history.value && version===generation && selectedConfig.value===config) history.value.runs = updatedHistory.runs

    }
  } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось обновить состояние' }
}
async function openHistory(id: number) {
  if(active.value !== 'whatsapp') return
  const version=generation
  selectedConfig.value = id
  await router.replace({path:'/settings',query:{section:'whatsapp',config:String(id)}})
  await action(async () => { const result = await api<HistoryPage>(`/settings/whatsapp/${id}/history/`); if(version===generation && selectedConfig.value===id) history.value=result }, '')
}
async function runAction(command: string, run_id?: number) {
  const id = selectedConfig.value
  if (!id) return
  await action(() => post(`/settings/whatsapp/${id}/history/`, {action: command, ...(run_id ? {run_id} : {})}), 'Команда импорта принята. Ход выполнения показан ниже и в очереди.')
  if(selectedConfig.value===id) await openHistory(id)
}
async function uploadExport() {
  if (!upload.value || !selectedConfig.value) return
  const form = new FormData(); form.set('file', upload.value); form.set('timezone', exportZone.value); form.set('date_order', dateOrder.value); form.set('request_key', requestKey())
  await action(() => api(`/settings/whatsapp/${selectedConfig.value}/history/`, {method: 'POST', body: form}), 'Файл принят в фоновую обработку.')
  if(selectedConfig.value && active.value==='whatsapp') await openHistory(selectedConfig.value)
}
function fileChosen(event: Event) { upload.value = (event.target as HTMLInputElement).files?.[0] ?? null }
function endpoint(row: SettingsRow) { return active.value === 'bitrix' ? '/settings/bitrix/' : `/settings/${active.value}/${row.id}/` }
const defaultHistory = {id: 0, enabled: true, only_new: false, new_message_poll_seconds: 5, interval_minutes: 0, analyze_after_import: true, page_size: 250, initial_wait_seconds: 180, poll_seconds: 30, stable_scans_required: 3}
watch(active, () => { selectedConfig.value = null; notice.value = ''; void load() })
watch(() => route.query.config, value => {
  const id=routeId(value)
  if(active.value === 'whatsapp' && id && selectedConfig.value !== id) void openHistory(id)
  else if(!id){selectedConfig.value=null;history.value=null}
})
onMounted(() => { void load(); timer = window.setInterval(refreshStatus, 5000) })
onBeforeUnmount(() => { generation++; if (timer) clearInterval(timer) })
</script>
<template>
  <section class="mx-panel-main overflow-y-auto" aria-label="Настройки">
    <header class="px-5 py-3 border-b border-[var(--mx-border)]"><h1 class="text-[23px] font-semibold">Настройки</h1>
      <nav class="flex gap-2 flex-wrap mt-3" aria-label="Настройки системы"><button v-for="section in sections" :key="section.id" class="mx-tab" :class="active === section.id ? 'mx-tab-active' : ''" @click="select(section.id)">{{ section.label }}</button><RouterLink to="/profile" class="mx-tab">Профиль и безопасность</RouterLink></nav>
    </header>
    <div class="p-4 md:p-5 max-w-5xl space-y-4">
      <Skeleton v-if="loading" :rows="5" />
      <ErrorState v-if="error" :text="error" :on-retry="load" />
      <p v-if="notice" class="mx-card p-3 text-emerald-300" role="status">{{ notice }} <RouterLink to="/queue" class="mx-link">Открыть очередь</RouterLink></p>
      <form v-if="access?.mfa_required" class="mx-card p-4 space-y-3 max-w-sm" @submit.prevent="challenge"><h2 class="font-semibold">Подтверждение MFA</h2><p class="mx-muted">Для привилегированных операций введите код из приложения.</p><label class="block">Одноразовый код<input v-model="code" class="mx-input mt-1" autocomplete="one-time-code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required /></label><button class="mx-btn mx-btn-primary" :disabled="busy">Подтвердить</button></form>
      <template v-else-if="!loading">
        <EmptyState v-if="!sections.length" title="Настройки интеграций недоступны" text="Личные настройки и активные сессии доступны в профиле." />
        <template v-if="data">
          <button v-if="(active === 'ai' && access?.can_create_ai) || (active === 'whatsapp' && access?.can_create_whatsapp) || (active === 'bitrix' && access?.can_create_bitrix && !data.items.length)" class="mx-btn" @click="creating = !creating">{{ creating ? 'Отменить создание' : 'Создать конфигурацию' }}</button>
          <form v-if="creating && active === 'ai'" class="mx-card p-4 space-y-3" @submit.prevent="createAiConfiguration"><h2 class="font-semibold">Новая конфигурация</h2><label class="block">Название<input v-model="newAiConfigName" class="mx-input mt-1" maxlength="120" required /></label><p class="mx-muted text-sm">Новая конфигурация создаётся выключенной. Настройте модель и ключи, затем включите её.</p><div class="flex gap-2"><button class="mx-btn mx-btn-primary" :disabled="busy">Создать</button><button type="button" class="mx-btn" @click="creating = false">Отмена</button></div></form>
          <article v-if="creating && active !== 'ai' && data.defaults" class="mx-card p-4 space-y-3"><h2 class="font-semibold">Новая конфигурация</h2><SettingsForm :fields="data.fields" :row="data.defaults" :endpoint="`/settings/${active}/`" method="POST" :teams="access?.teams" @saved="load" /></article>
          <EmptyState v-if="!data.items.length" title="Конфигураций нет" text="Для этого раздела ещё не создана конфигурация." />
          <template v-if="active === 'ai' && data.items.length">
            <section class="mx-card p-4 space-y-4" aria-label="Конфигурации ИИ">
              <header class="flex items-start justify-between gap-3 flex-wrap"><div><h2 class="font-semibold">Конфигурации ИИ</h2><p class="mx-muted text-sm mt-1">Выберите профиль для редактирования. Активные конфигурации могут участвовать в резервном маршруте.</p></div><span v-if="!data.items.some(row => row.is_active)" class="mx-chip">ИИ выключен</span></header>
              <div class="grid gap-2">
                <button v-for="config in data.items" :key="config.id" type="button" class="rounded-xl border p-3 text-left transition-colors" :class="selectedAiConfig === config.id ? 'border-violet-400/70 bg-violet-400/10' : 'border-[var(--mx-border)] hover:bg-white/5'" :aria-pressed="selectedAiConfig === config.id" @click="selectedAiConfig = config.id">
                  <span class="flex items-start justify-between gap-3"><span class="min-w-0"><strong class="block truncate">{{ config.name || `Конфигурация ${config.id}` }}</strong><small class="mx-muted block mt-1 truncate">{{ config.chat_model_name || 'Модель не задана' }}<span v-if="config.chat_provider_url"> · {{ safeProviderHost(config.chat_provider_url) }}</span></small></span><span class="flex gap-1.5 flex-wrap justify-end"><span class="mx-chip text-xs" :class="config.is_active ? 'text-emerald-300' : 'text-slate-400'">{{ config.is_active ? 'Активная' : 'Выключена' }}</span><span v-if="config.is_primary" class="mx-chip text-xs text-violet-200">Основная</span></span></span>
                </button>
              </div>
              <div v-if="selectedAiConfig !== null" class="flex items-center gap-3 flex-wrap border-t border-[var(--mx-border)] pt-4">
                <label class="flex items-center gap-2 text-sm"><input type="checkbox" class="w-4 h-4 accent-violet-400" :checked="Boolean(data.items.find(row => row.id === selectedAiConfig)?.is_active)" :disabled="data.can_change === false || busy" @change="setAiActive(data.items.find(row => row.id === selectedAiConfig)!, ($event.target as HTMLInputElement).checked)" />Активная конфигурация</label>
                <button v-if="!data.items.find(row => row.id === selectedAiConfig)?.is_primary" class="mx-btn" :disabled="data.can_change === false || busy" @click="setAiPrimary(data.items.find(row => row.id === selectedAiConfig)!)">Сделать основной</button>
                <span v-else class="mx-chip text-violet-200">Основная конфигурация</span>
              </div>
              <SettingsForm v-if="selectedAiConfig !== null && data.items.find(row => row.id === selectedAiConfig) && data.fields.some(field => field.name === 'name')" :fields="data.fields.filter(field => field.name === 'name')" :row="data.items.find(row => row.id === selectedAiConfig)!" :endpoint="`/settings/ai/${selectedAiConfig}/`" :readonly="data.can_change === false" @saved="load" />
            </section>
            <AiModelSettings v-if="data.items.find(row => row.id === selectedAiConfig)" :row="data.items.find(row => row.id === selectedAiConfig)!" :fields="data.fields.filter(field => field.name !== 'name')" :defaults="data.defaults" :readonly="data.can_change === false" />
          </template>
          <template v-if="active !== 'ai'" v-for="row in data.items" :key="row.id">
          <article class="mx-card p-4 space-y-4">
            <div class="flex gap-2 items-center flex-wrap"><h2 class="font-semibold text-base">{{ row.name }}</h2><StatusBadge v-if="typeof row.status === 'string'" :state="row.status" /><span v-if="row.connection_configured !== undefined" class="mx-chip">Подключение: {{ row.connection_configured ? 'Настроено' : 'Не настроено' }}</span></div>
            <SettingsForm :fields="data.fields" :row="row" :endpoint="endpoint(row)" :readonly="data.can_change === false" :teams="access?.teams" @saved="load" />
            <p v-if="typeof row.updated_at === 'string'" class="mx-muted text-xs">Конфигурация обновлена: {{ formatTime(row.updated_at) }}</p>
            <div v-if="row.keys" class="mx-muted text-sm">Ключ диалога: {{ row.keys.chat ? 'Настроено' : 'Не настроено' }} · Ключ векторизации: {{ row.keys.embedding ? 'Настроено' : 'Не настроено' }}</div>
            <template v-if="active === 'bitrix'"><ConnectionSecretForm v-for="key in ['webhook_url','inbound_token']" :key="key" endpoint="/settings/bitrix/" :kind="key" :label="key === 'webhook_url' ? 'REST webhook Bitrix24' : 'Токен входящего webhook'" :configured="Boolean(key === 'webhook_url' ? row.connection_configured : row.inbound_configured)" :readonly="data.can_change === false" @saved="load" /></template>
            <template v-if="active === 'whatsapp'">
              <div class="flex gap-2 flex-wrap"><button v-for="command in [{id:'status',name:'Обновить статус'},{id:'qr',name:'Запросить QR'},{id:'start',name:'Запустить сессию'},{id:'restart',name:'Перезапустить'},{id:'groups',name:'Обновить группы'}]" :key="command.id" class="mx-btn" :disabled="busy" @click="control(row,command.id)">{{ command.name }}</button><button class="mx-btn" @click="openHistory(row.id)">История и импорт</button></div>
              <img v-if="typeof row.qr_image === 'string'" :src="row.qr_image" alt="QR подключения WhatsApp" class="w-64 max-w-full bg-white rounded-lg" />
              <p v-if="row.qr_error" role="alert">{{ row.qr_error }}</p>
              <pre v-if="row.control" class="text-xs whitespace-pre-wrap break-words mx-muted">{{ row.control }}</pre>
            </template>
            <div v-if="active === 'bitrix' && row.runtime" class="mx-card p-3 space-y-2" data-testid="crm-settings-status">
              <p>Интеграция: {{ row.is_active ? 'включена' : 'выключена' }}</p><p>Автоматическая запись WhatsApp: {{ row.runtime.effective_autonomous_write_enabled ? 'включена' : 'выключена' }}</p>
              <p>Создание сделок: {{ row.auto_create_deals ? 'вкл.' : 'выкл.' }}</p><p v-if="row.disabled_reason">{{ row.disabled_reason }}</p>
              <RouterLink v-if="typeof row.ai_settings_href === 'string'" :to="row.ai_settings_href" class="mx-link">Настройки записи WhatsApp →</RouterLink>
            </div>
            <p v-if="row.last_sync_status" class="mx-muted">Последняя синхронизация: {{ row.last_sync_status }}</p>
            <p v-if="typeof row.last_sync_at === 'string'" class="mx-muted text-xs">Последняя загрузка CRM: {{ formatTime(row.last_sync_at) }}</p>
            <p v-if="typeof row.last_hourly_sync_at === 'string'" class="mx-muted text-xs">Последний запуск синхронизации: {{ formatTime(row.last_hourly_sync_at) }}</p>
          </article>
          </template>
          <div v-if="active === 'bitrix'" class="mx-card p-4 space-y-3"><h2 class="font-semibold">Обновить каталог CRM</h2><button v-for="team in access?.teams" :key="team.id" class="mx-btn mr-2" :disabled="busy" @click="action(() => post('/directory/crm-sync/', {team_id:team.id}), 'Синхронизация поставлена в очередь.')">Синхронизировать · {{ team.name }}</button></div>
        </template>
        <article v-if="history && selectedConfig" class="mx-card p-4 space-y-4"><h2 class="font-semibold">История WhatsApp</h2>
          <SettingsForm :fields="history.fields" :row="history.job ?? defaultHistory" :endpoint="`/settings/whatsapp/${selectedConfig}/history/`" @saved="openHistory(selectedConfig)" />
          <button class="mx-btn" :disabled="busy" @click="runAction('start')">Запустить импорт / мониторинг</button>
          <form class="grid gap-2" @submit.prevent="uploadExport"><h3>Импорт TXT-экспорта WhatsApp</h3><input type="file" accept=".txt,text/plain" aria-label="Файл экспорта" required @change="fileChosen" /><label>Часовой пояс<input v-model="exportZone" class="mx-input" required /></label><label>Порядок даты<select v-model="dateOrder" class="mx-select"><option value="DMY">День / месяц / год</option><option value="MDY">Месяц / день / год</option></select></label><button class="mx-btn justify-self-start" :disabled="busy || !upload">Импортировать файл</button></form>
          <article v-for="run in history.runs" :key="run.id" :data-run-id="run.id" data-testid="history-progress" :data-state="run.state" class="border-t border-[var(--mx-border)] pt-3 space-y-2"><div class="flex gap-2"><span>Запуск #{{ run.id }}</span><StatusBadge :state="run.state" /></div><p class="mx-muted">Всего: {{ run.progress.total }} · Обработано: {{ run.progress.processed }} · Ошибки: {{ run.progress.errors }} · Ожидают: {{ run.progress.pending }}</p><div class="flex gap-2 flex-wrap"><button v-for="command in [{id:'pause',name:'Пауза'},{id:'resume',name:'Продолжить'},{id:'cancel',name:'Отменить'},{id:'reanalyze',name:'Повторить анализ'}]" :key="command.id" class="mx-btn" :disabled="busy" @click="runAction(command.id, run.id)">{{ command.name }}</button></div></article>
        </article>
        <div v-if="qr" class="mx-card p-4"><h2>QR подключения WhatsApp</h2><img v-if="qr.startsWith('data:image/')" :src="qr" alt="QR подключения WhatsApp" class="max-w-64 bg-white" /><p v-else class="break-all">{{ qr }}</p></div>
        <WorkspaceView v-if="active === 'people'" initial-tab="people" />
        <template v-if="active === 'access' && memberData"><AccessGrants /><form class="mx-card p-4 grid gap-3 sm:grid-cols-2" @submit.prevent="action(async () => { await post('/access/', invite); await load() }, 'Доступ обновлён.')"><h2 class="font-semibold sm:col-span-2">Приглашение / отзыв доступа</h2><label>Телефон<input v-model="invite.phone" class="mx-input" pattern="[1-9][0-9]{9,14}" required /></label><label>Имя<input v-model="invite.name" class="mx-input" required /></label><label>Команда<select v-model="invite.team_id" class="mx-select"><option v-for="team in memberData.teams" :key="team.id" :value="team.id">{{ team.name }}</option></select></label><label>Роль<select v-model="invite.role" class="mx-select"><option value="manager">Менеджер</option><option value="team_lead">Руководитель</option><option value="finance">Финансист</option></select></label><label>Действие<select v-model="invite.status" class="mx-select"><option value="invited">Пригласить</option><option value="revoked">Отозвать</option></select></label><button class="mx-btn mx-btn-primary self-end justify-self-start" :disabled="busy">Сохранить доступ</button></form><div class="overflow-x-auto"><table class="mx-table"><thead><tr><th>Участник</th><th>Команда</th><th>Роль</th><th>Доступ</th></tr></thead><tbody><tr v-for="member in memberData.memberships" :key="member.id"><td>{{ member.user__username }}</td><td>{{ memberData.teams.find(t => t.id === member.team_id)?.name }}</td><td>{{ {manager:'Менеджер',team_lead:'Руководитель',finance:'Финансист'}[member.role] || member.role }}</td><td>{{ {active:'Активен',invited:'Приглашён',revoked:'Отозван'}[member.status] || member.status }}</td></tr></tbody></table></div></template>
        <DiagnosticsPanel v-if="active === 'system'" />
      </template>
    </div>
  </section>
</template>
