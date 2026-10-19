<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Cpu, MessageSquare, Network } from 'lucide-vue-next'
import { api, post } from '../../composables/api'
import ConnectionSecretForm from '../../shell/ConnectionSecretForm.vue'
import type { AiConnectionCheck, AiModelRole, AiSettingsGroup, ChatModelProfile, SettingField, SettingValue, SettingsPage, SettingsRow } from '../../types/settings'
import AiSettingsFields from './AiSettingsFields.vue'
import { parseProfile, safeProfile } from './aiProfile'

const props = defineProps<{ role: AiModelRole; row: SettingsRow; fields: SettingField[]; defaults?: SettingsRow; readonly?: boolean; decisionEnabled: boolean }>()
const emit = defineEmits<{ saved: [row: SettingsRow]; decision: [enabled: boolean] }>()
const draft = ref<Record<string, SettingValue>>({}), profile = ref<ChatModelProfile | null>(null)
const profileName = ref(''), profileError = ref(''), error = ref(''), notice = ref(''), busy = ref(false), checking = ref(false)
const qualityTouched = ref(false)
const check = ref<AiConnectionCheck | null>(null), checkError = ref('')
const endpoint = computed(() => `/settings/ai/${props.row.id}/models/${props.role}/`)
const title = computed(() => ({ chat: 'Чат-модель', embedding: 'Embedding-модель', decision: 'Модель принятия решений' })[props.role])
const description = computed(() => props.role === 'chat' ? (props.decisionEnabled ? 'Извлечение фактов, анализ переписки и ответы ассистента' : 'Весь цикл: распределение по темам, принятие решений, анализ и ответы') : props.role === 'embedding' ? 'Семантический поиск и сравнение сообщений' : 'Распределение по темам, классификация и выбор вариантов')
const enabled = computed(() => props.role !== 'decision' || draft.value.decision_enabled === true)
const primaryNames = computed(() => [`${props.role}_provider_url`, `${props.role}_model_name`, `${props.role}_api_format`])
const primary = computed(() => primaryNames.value.flatMap(name => props.fields.filter(field => field.name === name)))
const groups = computed<AiSettingsGroup[]>(() => props.role === 'chat' ? [
  { title: 'Контекст и генерация', names: ['chat_temperature', 'context_window_tokens', 'max_completion_tokens', 'context_safety_tokens', 'analysis_input_token_limit', 'analysis_target_message_limit', 'analysis_output_token_limit'] },
  { title: 'Токенизатор', names: ['tokenizer_id', 'tokenizer_revision'] },
  { title: 'Инструкции для обработки и ассистента', names: ['system_prompt_worker', 'system_prompt_assistant'] },
] : props.role === 'embedding' ? [{ title: 'Параметры векторизации', names: ['embedding_dimension'] }] : [
  { title: 'Правила принятия решений', names: ['thread_routing_mode', 'laya_match_threshold', 'laya_match_margin', 'decision_model_revision', 'decision_calibration_revision', 'decision_quality_confirmed', 'routing_policy_version'] },
  { title: 'Маршрут запроса и ограничения', names: ['decision_request_path', 'decision_timeout_seconds'] },
])
const extra = computed(() => props.fields.filter(field => !primaryNames.value.includes(field.name) && field.name !== 'decision_enabled' && field.name !== 'chat_model_profile' && !groups.value.some(group => group.names.includes(field.name))))
const dirty = computed(() => props.fields.some(field => field.name !== 'chat_model_profile' && draft.value[field.name] !== props.row[field.name]) || (props.role === 'chat' && JSON.stringify(profile.value) !== JSON.stringify(safeProfile(props.row.chat_model_profile))))
let pollTimer: number | undefined
let controller: AbortController | undefined
let disposed = false
let attempts = 0
function reset(row: SettingsRow) {
  draft.value = Object.fromEntries(props.fields.filter(field => field.name !== 'chat_model_profile').map(field => {
    const value = row[field.name]
    return [field.name, typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null ? value : '']
  }))
  profile.value = safeProfile(row.chat_model_profile)
  profileName.value = ''; qualityTouched.value = false
  if (props.role === 'decision' && row.decision_enabled === true && ['llm', 'laya'].includes(String(draft.value.thread_routing_mode))) draft.value.thread_routing_mode = 'decision'
  if (props.role === 'decision') emit('decision', draft.value.decision_enabled === true)
}
watch(() => props.row.id, () => reset(props.row), { immediate: true })
function change(name: string, value: SettingValue) {
  draft.value[name] = value; notice.value = ''; check.value = null; checkError.value = ''
  if (props.role === 'decision') {
    if (name === 'decision_quality_confirmed') qualityTouched.value = true
    else if (name !== 'decision_enabled') { draft.value.decision_quality_confirmed = false; qualityTouched.value = false }
  }
  if (name === 'decision_api_format' && ['/v1/systemone', '/chat/completions'].includes(String(draft.value.decision_request_path))) {
    draft.value.decision_request_path = value === 'openai_compatible' ? '/chat/completions' : '/v1/systemone'
  }
  if (name === 'decision_enabled') {
    if (value === true && ['llm', 'laya'].includes(String(draft.value.thread_routing_mode))) draft.value.thread_routing_mode = 'decision'
    emit('decision', value === true)
  }
}
async function loadSaved() {
  const page = await api<SettingsPage>('/settings/ai/')
  const row = page.items.find(item => item.id === props.row.id)
  if (!row) throw new Error('Конфигурация больше недоступна. Обновите страницу.')
  reset(row); emit('saved', row)
}
async function keySaved() {
  busy.value = true; error.value = ''; notice.value = ''; check.value = null; checkError.value = ''
  try { await loadSaved(); notice.value = 'API-ключ обновлён' }
  catch (e) { error.value = e instanceof Error ? e.message : 'Ключ изменён, но не удалось обновить настройки. Обновите страницу.' }
  finally { busy.value = false }
}
async function save() {
  busy.value = true; error.value = ''; notice.value = ''; check.value = null
  try {
    const payload: Record<string, unknown> = Object.fromEntries(Object.entries(draft.value).filter(([name, value]) => value !== props.row[name]))
    if (props.role === 'decision' && !qualityTouched.value && payload.decision_quality_confirmed === true) delete payload.decision_quality_confirmed
    if (props.role === 'decision' && qualityTouched.value) payload.decision_quality_confirmed = draft.value.decision_quality_confirmed
    if (props.role === 'chat' && JSON.stringify(profile.value) !== JSON.stringify(safeProfile(props.row.chat_model_profile))) payload.chat_model_profile = profile.value ?? {}
    if (!Object.keys(payload).length) return
    await api(endpoint.value, { method: 'PATCH', body: JSON.stringify(payload) })
    await loadSaved(); notice.value = 'Настройки модели сохранены'
  } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось сохранить настройки модели' }
  finally { busy.value = false }
}
async function uploadProfile(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  input.value = ''; profileError.value = ''
  if (!file) return
  if (file.size > 16 * 1024 * 1024) { profileError.value = 'Размер JSON-профиля не должен превышать 16 МБ.'; return }
  try { profile.value = parseProfile(JSON.parse(await file.text()) as unknown); profileName.value = file.name; notice.value = ''; check.value = null; checkError.value = '' }
  catch (e) { profileError.value = e instanceof SyntaxError ? 'Не удалось прочитать JSON. Проверьте формат файла.' : e instanceof Error ? e.message : 'Недопустимый профиль' }
}
function exportProfile() {
  const saved = safeProfile(props.row.chat_model_profile)
  if (!saved) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(saved, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a'); link.href = url; link.download = `mazory-chat-profile-${props.row.id}.json`; link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function poll(operationId: number) {
  try {
    const result = await api<AiConnectionCheck>(`${endpoint.value}check/?operation_id=${operationId}`, { signal: controller?.signal })
    if (disposed) return
    check.value = result
    if (['completed', 'failed'].includes(result.status)) { checking.value = false; return }
    if (++attempts >= 180) throw new Error('Проверка ещё выполняется. Повторите проверку позже.')
    pollTimer = window.setTimeout(() => { void poll(operationId) }, 1500)
  } catch (e) { if (!disposed) { checkError.value = e instanceof Error ? e.message : 'Не удалось получить результат проверки'; checking.value = false } }
}
async function checkConnection() {
  checking.value = true; check.value = null; checkError.value = ''; attempts = 0; controller = new AbortController()
  try {
    const operation = await post<{ operation_id: number }>(`${endpoint.value}check/`, {})
    if (!disposed) await poll(operation.operation_id)
  } catch (e) { if (!disposed) { checkError.value = e instanceof Error ? e.message : 'Не удалось запустить проверку'; checking.value = false } }
}
const checkMessages: Record<string, string> = {
  provider_unavailable: 'Провайдер недоступен.', provider_connection_failed: 'Не удалось подключиться к провайдеру.',
  provider_invalid_response: 'Провайдер вернул некорректный ответ.', provider_authentication_failed: 'Провайдер отклонил ключ доступа.',
  credential_decryption_failed: 'Не удалось прочитать сохранённый ключ. Сохраните ключ повторно.',
  decision_disabled: 'Модель принятия решений отключена.', decision_invalid_response: 'Модель решений вернула некорректный ответ.',
  decision_model_changed: 'Модель решений изменилась во время проверки. Запустите её заново.',
  ai_not_configured: 'Укажите маршрут и модель, затем сохраните настройки.', embedding_dimension_mismatch: 'Размерность ответа не совпадает с настройками векторизации.',
  configuration_changed: 'Настройки изменились во время проверки. Запустите её заново.',
}
const resultDetails = computed(() => {
  const result = check.value?.result
  if (!result) return ''
  return [['model', 'Модель'], ['dimension', 'Размерность'], ['latency_ms', 'Время, мс']].map(([key, label]) => key && result[key] !== undefined ? `${label}: ${String(result[key])}` : '').filter(Boolean).join(' · ')
})
onBeforeUnmount(() => { disposed = true; controller?.abort(); if (pollTimer !== undefined) window.clearTimeout(pollTimer) })
</script>
<template>
  <article :id="`ai-${row.id}-${role}`" class="mx-card overflow-hidden" :data-model-role="role">
    <header class="p-4 md:p-6 flex gap-4 justify-between flex-wrap">
        <div class="flex items-start gap-3 min-w-0"><span class="ai-model-mark"><component :is="role === 'chat' ? MessageSquare : role === 'embedding' ? Network : Cpu" :size="18" /></span><div><h3 class="text-lg font-semibold">{{ title }}</h3><p class="mx-muted text-xs mt-1">{{ description }}</p></div></div>
        <div class="flex items-center gap-3 flex-wrap"><label v-if="role === 'decision'" class="flex items-center gap-2 text-xs"><input type="checkbox" :checked="enabled" :disabled="readonly || busy || checking" class="w-4 h-4 accent-violet-400" :aria-controls="`ai-${row.id}-decision-fields`" @change="change('decision_enabled', ($event.target as HTMLInputElement).checked)" />Использовать модель</label><span class="mx-chip text-xs">{{ dirty ? 'Есть изменения' : !enabled ? 'Отключена' : 'Настройки сохранены' }}</span></div>
      </header>
      <p v-if="role === 'decision'" class="mx-4 md:mx-6 mb-5 p-3 rounded-lg border border-[var(--mx-border)] text-xs" role="status"><strong class="block mb-1">{{ enabled ? 'Модель решений → чат-модель' : 'Весь цикл → чат-модель' }}</strong><span class="mx-muted">{{ enabled ? 'Классификация и выбор отдельно, анализ и ответ — в чат-модели.' : 'Отдельная модель решений не вызывается. Настройки сохранены для повторного включения.' }}{{ dirty ? ' Маршрут применяется после сохранения.' : '' }}</span></p>
      <fieldset :id="`ai-${row.id}-${role}-fields`" :disabled="readonly || busy || checking || !enabled" class="min-w-0">
        <div class="px-4 md:px-6 pb-5"><AiSettingsFields :fields="primary" :values="draft" :defaults="defaults" :form="`ai-${row.id}-${role}-form`" required @change="change" /></div>
      </fieldset>
    <div class="px-4 md:px-6 pb-5"><ConnectionSecretForm :endpoint="`/settings/ai/${row.id}/`" :kind="role" :label="`API-ключ · ${title}`" :configured="Boolean(row.keys?.[role])" :readonly="readonly || busy || checking || !enabled" @saved="keySaved" /></div>
    <form :id="`ai-${row.id}-${role}-form`" @submit.prevent="save">
      <fieldset :disabled="readonly || busy || checking || !enabled" class="min-w-0">
        <details v-for="group in groups.filter(group => fields.some(field => group.names.includes(field.name)))" :key="group.title" class="border-t border-[var(--mx-border)]"><summary class="px-4 md:px-6 py-4 text-sm cursor-pointer">{{ group.title }}</summary><div class="px-4 md:px-6 pb-5"><AiSettingsFields :fields="fields.filter(field => group.names.includes(field.name))" :values="draft" :defaults="defaults" @change="change" /><p v-if="role === 'embedding'" class="mx-muted text-xs mt-3">Смена модели или размерности требует перестроения векторного индекса. Оно не запускается автоматически; существующие данные сохраняются.</p></div></details>
        <details v-if="role === 'chat'" class="border-t border-[var(--mx-border)]"><summary class="px-4 md:px-6 py-4 text-sm cursor-pointer">Профиль модели · квантование и токенизатор</summary><div class="px-4 md:px-6 pb-5 space-y-3"><div class="p-4 border border-dashed border-[var(--mx-border)] rounded-lg space-y-2"><strong class="text-sm">Конфигурация модели JSON</strong><p class="mx-muted text-xs">Профиль версии 1: квантование, содержимое tokenizer.json, шаблон чата и окно контекста. До 16 МБ.</p><label class="mx-btn inline-flex text-xs cursor-pointer">Загрузить JSON-профиль<input type="file" accept=".json,application/json" class="sr-only" @change="uploadProfile" /></label></div><p v-if="profile" class="text-xs mx-muted break-words">{{ profileName || 'Сохранённый профиль' }} · версия {{ profile.version }}{{ profile.tokenizer ? ' · данные токенизатора' : '' }}{{ profile.quantization ? ' · метаданные квантования' : '' }}{{ profile.context_window_tokens ? ` · контекст ${profile.context_window_tokens} токенов` : '' }}{{ profile.chat_template ? ' · шаблон чата' : '' }}</p><div class="flex gap-2 flex-wrap"><button type="button" class="mx-btn" :disabled="!safeProfile(row.chat_model_profile)" @click="exportProfile">Экспорт сохранённого профиля</button><button v-if="profile && !readonly" type="button" class="mx-btn" @click="profile = null; profileName = ''; check = null">Удалить профиль при сохранении</button></div><p class="mx-muted text-xs">Квантование описывает модель у провайдера. Загрузка профиля не меняет веса модели. Изменения вступят в силу после сохранения чат-модели.</p><p v-if="profileError" role="alert" class="text-rose-300 text-xs">{{ profileError }}</p></div></details>
        <details v-if="extra.length" class="border-t border-[var(--mx-border)]"><summary class="px-4 md:px-6 py-4 text-sm cursor-pointer">Дополнительные параметры</summary><div class="px-4 md:px-6 pb-5"><AiSettingsFields :fields="extra" :values="draft" :defaults="defaults" @change="change" /></div></details>
      </fieldset>
      <div class="border-t border-[var(--mx-border)] p-4 md:px-6 space-y-3">
        <p v-if="error" role="alert" class="text-rose-300 text-sm whitespace-pre-wrap">{{ error }}</p><p v-if="notice" role="status" class="text-emerald-300 text-sm">{{ notice }}</p>
        <div class="flex flex-wrap items-center gap-2 justify-end"><span v-if="dirty" class="mx-muted text-xs mr-auto">Сохраните изменения перед проверкой подключения</span><button type="button" class="mx-btn" :disabled="readonly || busy || checking || dirty || !enabled" @click="checkConnection">{{ checking ? 'Проверка выполняется…' : 'Проверить подключение' }}</button><button v-if="!readonly" type="submit" class="mx-btn mx-btn-primary" :disabled="busy || checking || !dirty">{{ busy ? 'Сохранение…' : 'Сохранить модель' }}</button></div>
        <div aria-live="polite" class="text-xs"><p v-if="check?.status === 'queued' || check?.status === 'running'" class="mx-muted">{{ check.status === 'queued' ? 'Проверка в очереди. Ожидаем ответ провайдера.' : 'Отправлен реальный запрос провайдеру. Ожидаем результат.' }}</p><p v-else-if="check?.status === 'completed'" class="text-emerald-300">Подключение проверено: провайдер ответил на запрос. {{ resultDetails }}<span v-if="role === 'decision'" class="block mx-muted mt-1">Качество решений и калибровка этой проверкой не подтверждаются.</span></p><p v-else-if="check?.status === 'failed'" class="text-rose-300">Проверка завершилась ошибкой. {{ checkMessages[check.error_code] ?? 'Проверьте маршрут, модель и ключ доступа.' }}</p><p v-if="checkError" role="alert" class="text-rose-300">{{ checkError }}</p></div>
      </div>
    </form>
  </article>
</template>
<style scoped>
.ai-model-mark { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: 9px; background: color-mix(in srgb, var(--mx-accent, #ac8cff) 10%, transparent); color: var(--mx-accent, #ac8cff); }
fieldset:disabled { opacity: .65; }
</style>
