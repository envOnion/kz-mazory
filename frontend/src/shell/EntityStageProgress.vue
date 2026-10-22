<script setup lang="ts">
import { computed } from 'vue'
import type { EntityStageProgress } from '../types/messenger'
const props = defineProps<{ value: unknown; label?: string }>()
function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function count(value: unknown): number { return typeof value === 'number' && Number.isFinite(value) ? value : 0 }
function tokens(value: unknown): number | null { return typeof value === 'number' && Number.isFinite(value) ? value : null }
const progress = computed(() => {
  const value = record(props.value)
  if (value.policy !== 'entity-stages-v1' || !Array.isArray(value.stages)) return null
  const stages: EntityStageProgress[] = value.stages.map((entry: unknown) => {
    const row = record(entry), metrics = record(row.metrics), usage = record(row.usage)
    return { stage: typeof row.stage === 'string' ? row.stage : '', state: typeof row.state === 'string' ? row.state : '',
      attempts: count(row.attempts), error: typeof row.error === 'string' ? row.error : '', wireBytes: count(metrics.wire_bytes),
      inputTokens: tokens(usage.prompt_tokens ?? usage.input_tokens), outputTokens: tokens(usage.completion_tokens ?? usage.output_tokens),
      reasoningTokens: tokens(row.reasoning_tokens) }
  })
  return { calls: count(value.calls), completed: count(value.completed_steps), stages,
    error: typeof value.error === 'string' ? value.error : '',
    input: stages.every(row => row.inputTokens !== null) ? stages.reduce((total, row) => total + (row.inputTokens ?? 0), 0) : null,
    output: stages.every(row => row.outputTokens !== null) ? stages.reduce((total, row) => total + (row.outputTokens ?? 0), 0) : null }
})
</script>
<template>
  <div v-if="progress" class="mx-card mt-2 p-3">
    <p v-if="label" class="mx-muted">{{ label }}</p>
    <p>Сохранено этапов: {{ progress.completed }} · вызовов: {{ progress.calls }}</p>
    <p v-if="progress.error" class="text-amber-300">Обработка остановлена: {{ progress.error }}</p>
    <p class="mx-muted">Токены: вход {{ progress.input ?? 'неизвестно' }}, выход {{ progress.output ?? 'неизвестно' }}. Применение фактов проверяется отдельно.</p>
    <details class="mt-2 text-xs">
      <summary>Этапы и расход</summary>
      <ul class="mt-2 space-y-1">
        <li v-for="(stage, index) in progress.stages" :key="`${stage.stage}:${index}`">
          {{ stage.stage }} · {{ stage.state === 'done' ? 'сохранён' : 'не завершён' }} · {{ stage.wireBytes }} байт · {{ stage.inputTokens ?? '—' }}/{{ stage.outputTokens ?? '—' }} токенов · reasoning {{ stage.reasoningTokens ?? '—' }}
          <span v-if="stage.error" class="text-amber-300"> · {{ stage.error }}</span>
        </li>
      </ul>
    </details>
  </div>
</template>
