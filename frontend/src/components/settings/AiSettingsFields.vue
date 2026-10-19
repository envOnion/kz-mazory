<script setup lang="ts">
import type { SettingField, SettingValue, SettingsRow } from '../../types/settings'
const props = defineProps<{ fields: SettingField[]; values: Record<string, SettingValue>; defaults?: SettingsRow; required?: boolean; form?: string }>()
const emit = defineEmits<{ change: [name: string, value: SettingValue] }>()
const labels: Record<string, string> = {
  chat_provider_url: 'Маршрут · Base URL', chat_model_name: 'Модель', chat_api_format: 'Протокол',
  embedding_provider_url: 'Маршрут · Base URL', embedding_model_name: 'Модель', embedding_dimension: 'Размерность вектора',
  decision_provider_url: 'Маршрут · Base URL', decision_model_name: 'Модель', decision_api_format: 'Протокол',
  decision_request_path: 'Путь метода принятия решений', decision_timeout_seconds: 'Таймаут, секунды',
  decision_model_revision: 'Версия модели', decision_calibration_revision: 'Версия калибровки',
  decision_quality_confirmed: 'Калибровка модели проверена', thread_routing_mode: 'Режим работы',
  laya_match_threshold: 'Минимальная уверенность', laya_match_margin: 'Минимальный разрыв вариантов',
  chat_temperature: 'Температура', max_completion_tokens: 'Резерв ответа, токены',
  context_safety_tokens: 'Технический запас, токены', autonomous_max_in_flight: 'Одновременных обработок',
  daily_budget_usd: 'Бюджет в сутки, USD', minute_request_limit: 'Запросов в минуту', daily_request_limit: 'Запросов в сутки',
  autonomous_daily_token_limit: 'Токенов в сутки для автономной обработки',
  autonomous_policy_version: 'Версия политики автономной обработки', autonomous_context_messages: 'Сообщений контекста',
  autonomous_input_tokens: 'Вход автономной обработки, токены', system_prompt_worker: 'Инструкция для обработки', system_prompt_assistant: 'Инструкция ассистента',
}
function choices(field: SettingField) {
  return field.name === 'thread_routing_mode'
    ? field.choices.filter(choice => !['llm', 'laya'].includes(choice.value)).map(choice => ({ ...choice, label: choice.value === 'shadow' ? 'Наблюдение · без применения' : choice.value === 'decision' ? 'Принимать решения' : choice.label }))
    : field.choices
}
function input(field: SettingField, event: Event) {
  const element = event.target as HTMLInputElement
  const value = ['integer', 'number'].includes(field.type) ? (element.value === '' ? '' : element.valueAsNumber) : element.value
  emit('change', field.name, value)
}
function defaultText(field: SettingField): string {
  const value = props.defaults?.[field.name]
  if (value === undefined || value === null || value === '') return ''
  if (field.type === 'text' || field.type === 'json') return ''
  if (typeof value === 'boolean') return value ? 'Включено' : 'Выключено'
  if (field.name === 'thread_routing_mode' && value === 'llm') return 'Весь цикл → чат-модель'
  return field.choices.find(choice => choice.value === value)?.label ?? String(value)
}
</script>
<template>
  <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
    <label v-for="field in fields" :key="field.name" class="min-w-0 flex flex-col gap-1.5" :class="field.type === 'text' ? 'sm:col-span-2 lg:col-span-3' : ''">
      <span class="text-xs font-medium">{{ labels[field.name] ?? field.label }}</span>
      <input :form="form" v-if="field.type === 'boolean'" type="checkbox" :checked="values[field.name] === true" class="w-5 h-5 accent-violet-400" @change="emit('change', field.name, ($event.target as HTMLInputElement).checked)" />
      <select :form="form" v-else-if="field.choices.length" :value="values[field.name]" class="mx-select" @change="input(field, $event)"><option v-for="choice in choices(field)" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select>
      <textarea :form="form" v-else-if="field.type === 'text'" :value="String(values[field.name] ?? '')" class="mx-input min-h-28" rows="5" @input="input(field, $event)" />
      <input :form="form" v-else :value="values[field.name]" :type="field.name.endsWith('_provider_url') ? 'url' : ['integer','number','decimal'].includes(field.type) ? 'number' : 'text'" :step="field.type === 'integer' ? '1' : 'any'" :min="['integer','number','decimal'].includes(field.type) ? 0 : undefined" :maxlength="field.max_length ?? undefined" :required="required && (field.name.endsWith('_provider_url') || field.name.endsWith('_model_name'))" class="mx-input" :spellcheck="false" @input="input(field, $event)" />
      <small v-if="field.name === 'decision_quality_confirmed'" class="mx-muted text-xs">Подтверждение администратора: качество модели и калибровка проверены. Проверка подключения подтверждает только обмен с провайдером. Для активных решений нужны обе версии и подтверждение калибровки.</small>
      <small v-else-if="field.help && !required" class="mx-muted text-xs">{{ field.help }}</small>
      <small v-if="!required && defaultText(field)" class="mx-muted text-[11px]">По умолчанию: {{ defaultText(field) }}</small>
    </label>
  </div>
</template>
