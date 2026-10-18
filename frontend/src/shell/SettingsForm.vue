<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../composables/api'
import type { SettingField, SettingValue, SettingsRow } from '../types/settings'
const props = defineProps<{ fields: SettingField[]; row: SettingsRow; endpoint: string; readonly?: boolean; method?: 'POST' | 'PATCH'; teams?: {id: number; name: string}[] }>()
const emit = defineEmits<{ saved: [] }>()
const draft = ref<Record<string, SettingValue>>({})
const error = ref(''), busy = ref(false), saved = ref(false)
watch(() => [props.row, props.fields], () => {
  const values: Record<string, SettingValue> = {}
  for (const field of props.fields) {
    const value = props.row[field.name]
    values[field.name] = typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null ? value : ''
  }
  draft.value = values
}, { immediate: true })
async function save() {
  busy.value = true; error.value = ''; saved.value = false
  const changed = props.row.id === 0 ? {...draft.value} : Object.fromEntries(Object.entries(draft.value).filter(([key, value]) => value !== props.row[key]))
  if (!Object.keys(changed).length) { busy.value = false; return }
  try { await api(props.endpoint, {method: props.method || 'PATCH', body: JSON.stringify(changed)}); saved.value = true; emit('saved') }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось сохранить' }
  finally { busy.value = false }
}
</script>
<template>
  <form class="grid gap-3" @submit.prevent="save">
    <fieldset :disabled="readonly" class="grid gap-3 sm:grid-cols-2">
      <label v-for="field in fields" :key="field.name" class="min-w-0 flex flex-col gap-1" :class="field.type === 'text' ? 'sm:col-span-2' : ''">
        <span>{{ field.label }}</span>
        <input v-if="field.type === 'boolean'" v-model="draft[field.name]" :aria-label="field.label" type="checkbox" class="w-5 h-5 accent-violet-400" />
        <select v-else-if="field.name === 'team'" v-model="draft[field.name]" :aria-label="field.label" class="mx-select"><option :value="null">Без команды</option><option v-for="team in teams" :key="team.id" :value="team.id">{{ team.name }}</option></select>
        <select v-else-if="field.choices.length" v-model="draft[field.name]" :aria-label="field.label" class="mx-select"><option v-for="choice in field.choices" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select>
        <textarea v-else-if="field.type === 'text'" :value="String(draft[field.name] ?? '')" :aria-label="field.label" class="mx-input" rows="5" @input="draft[field.name] = ($event.target as HTMLTextAreaElement).value" />
        <input v-else-if="['integer','number'].includes(field.type)" v-model.number="draft[field.name]" :aria-label="field.label" type="number" :step="field.type === 'integer' ? '1' : 'any'" class="mx-input" />
        <input v-else v-model="draft[field.name]" :aria-label="field.label" :maxlength="field.max_length ?? undefined" class="mx-input" :inputmode="field.type === 'decimal' ? 'decimal' : undefined" />
        <small v-if="field.help" class="mx-muted">{{ field.help }}</small>
      </label>
    </fieldset>
    <p v-if="error" role="alert" class="text-rose-300 whitespace-pre-wrap">{{ error }}</p>
    <p v-if="saved" role="status" class="text-emerald-300">Настройки сохранены</p>
    <button v-if="!readonly" class="mx-btn mx-btn-primary justify-self-start" :disabled="busy" type="submit">{{ busy ? 'Сохранение…' : 'Сохранить настройки' }}</button>
  </form>
</template>
