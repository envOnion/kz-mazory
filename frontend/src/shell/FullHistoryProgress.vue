<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ value: unknown }>()
const progress = computed(() => {
  if (!props.value || typeof props.value !== 'object') return null
  const data = props.value as Record<string,unknown>
  if (typeof data.total !== 'number' || typeof data.validated_read !== 'number') return null
  return {total:data.total,read:data.validated_read,complete:data.complete === true,stage:typeof data.stage === 'number' ? data.stage : null}
})
</script>
<template>
  <div v-if="progress" data-testid="history-progress" class="mx-card p-3 mt-2">
    <p>Прочитано {{ progress.read }} из {{ progress.total }} · этап {{ progress.stage ?? 'неизвестен' }}</p>
    <p v-if="progress.complete" class="text-emerald-300">Полный разбор завершён: все оригиналы прочитаны и итог сверен.</p>
    <p v-else class="mx-muted">Разбор продолжается. Промежуточный результат не считается полным.</p>
  </div>
</template>
