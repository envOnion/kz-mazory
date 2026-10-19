<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Cpu, MessageSquare, Network, Download } from 'lucide-vue-next'
import { api } from '../../composables/api'
import type { AiModelRole, AiSettingsRole, SettingField, SettingsPage, SettingsRow } from '../../types/settings'
import SettingsForm from '../../shell/SettingsForm.vue'
import AiModelCard from './AiModelCard.vue'
import { safeJson, safeProfile } from './aiProfile'

const props = defineProps<{ row: SettingsRow; fields: SettingField[]; defaults?: SettingsRow; readonly?: boolean }>()
const current = ref<SettingsRow>({ ...props.row }), decisionEnabled = ref(props.row.decision_enabled === true)
const sharedRow = ref<SettingsRow>({ ...props.row })
const error = ref(''), notice = ref(''), exporting = ref(false)
const roles: AiModelRole[] = ['chat', 'embedding', 'decision']
const chatNames = ['context_window_tokens', 'max_completion_tokens', 'context_safety_tokens', 'tokenizer_id', 'tokenizer_revision', 'system_prompt_worker', 'system_prompt_assistant', 'analysis_input_token_limit', 'analysis_target_message_limit', 'analysis_output_token_limit']
const decisionNames = ['thread_routing_mode', 'laya_match_threshold', 'laya_match_margin', 'routing_policy_version']
function roleFor(name: string): AiSettingsRole {
  if (name.startsWith('chat_') || chatNames.includes(name)) return 'chat'
  if (name.startsWith('embedding_')) return 'embedding'
  if (name.startsWith('decision_') || decisionNames.includes(name)) return 'decision'
  return 'shared'
}
const byRole = computed(() => Object.fromEntries((['chat', 'embedding', 'decision', 'shared'] as const).map(role => [role, props.fields.filter(field => roleFor(field.name) === role)])) as Record<AiSettingsRole, SettingField[]>)
watch(() => props.row.id, () => { current.value = { ...props.row }; sharedRow.value = { ...props.row }; decisionEnabled.value = props.row.decision_enabled === true })
async function refresh() {
  error.value = ''
  try {
    const page = await api<SettingsPage>('/settings/ai/')
    const row = page.items.find(item => item.id === props.row.id)
    if (!row) throw new Error('Конфигурация больше недоступна. Обновите страницу.')
    current.value = row
  } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось обновить конфигурацию' }
}
async function exportSettings() {
  exporting.value = true; error.value = ''; notice.value = ''
  try {
    const page = await api<SettingsPage>('/settings/ai/')
    const row = page.items.find(item => item.id === props.row.id)
    if (!row) throw new Error('Конфигурация больше недоступна.')
    const models = Object.fromEntries(roles.map(role => [role, Object.fromEntries(byRole.value[role].filter(field => field.name !== 'chat_model_profile' && !/(?:key|secret|token_encrypted)/i.test(field.name)).map(field => [field.name, safeJson(row[field.name])]))]))
    const profile = safeProfile(row.chat_model_profile)
    const configuration = { version: 1, configuration_id: row.id, models: { ...models, chat: { ...models.chat, ...(profile ? { chat_model_profile: profile } : {}) } }, shared: Object.fromEntries(byRole.value.shared.filter(field => !/(?:key|secret|token_encrypted)/i.test(field.name)).map(field => [field.name, safeJson(row[field.name])])), routing: { mode: row.decision_enabled ? 'decision_and_chat' : 'chat_only' } }
    const url = URL.createObjectURL(new Blob([JSON.stringify(safeJson(configuration), null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `mazory-ai-${row.id}.json`; link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    notice.value = 'Экспортированы сохранённые настройки. API-ключи исключены.'
  } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось экспортировать конфигурацию' }
  finally { exporting.value = false }
}
async function refreshShared() { await refresh(); sharedRow.value = { ...current.value } }
</script>
<template>
  <section class="space-y-5" :aria-label="`Модели ИИ · ${String(current.name ?? '')}`">
    <header class="flex gap-3 justify-between items-start flex-wrap"><div><h2 class="font-semibold text-xl">{{ current.name }}</h2><p class="mx-muted text-xs mt-1">Независимые подключения и параметры для каждой задачи</p></div><button class="mx-btn flex gap-2 items-center" :disabled="exporting" @click="exportSettings"><Download :size="15" />{{ exporting ? 'Экспорт…' : 'Экспорт JSON' }}</button></header>
    <p v-if="error" role="alert" class="text-rose-300 text-sm">{{ error }}</p><p v-if="notice" role="status" class="text-emerald-300 text-sm">{{ notice }}</p>
    <nav class="grid grid-cols-3 gap-2 md:gap-3" aria-label="Модели конфигурации"><a v-for="role in roles" :key="role" :href="`#ai-${row.id}-${role}`" class="mx-card p-3 md:p-4 flex flex-col md:flex-row gap-2 md:gap-3 items-center text-center md:text-left"><component :is="role === 'chat' ? MessageSquare : role === 'embedding' ? Network : Cpu" :size="19" class="text-violet-300 shrink-0" /><span class="min-w-0"><strong class="text-xs md:text-sm font-medium">{{ { chat: 'Чат-модель', embedding: 'Embedding-модель', decision: 'Решения' }[role] }}</strong><small class="mx-muted hidden md:block text-[11px] mt-1">{{ role === 'chat' ? 'Анализ и ответы' : role === 'embedding' ? 'Поиск и сходство' : decisionEnabled ? 'Классификация и выбор' : 'Отключена · работает чат' }}</small></span></a></nav>
    <AiModelCard v-for="role in roles" :key="`${row.id}-${role}`" :role="role" :row="current" :fields="byRole[role]" :defaults="defaults" :readonly="readonly" :decision-enabled="decisionEnabled" @saved="current = $event" @decision="decisionEnabled = $event" />
    <details class="mx-card"><summary class="px-4 md:px-6 py-4 cursor-pointer font-medium text-sm">Общие настройки обработки</summary><div class="px-4 md:px-6 pb-5"><SettingsForm :fields="byRole.shared" :row="sharedRow" :endpoint="`/settings/ai/${row.id}/models/shared/`" :readonly="readonly" @saved="refreshShared" /></div></details>
  </section>
</template>
