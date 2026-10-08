<template>
  <section class="space-y-4" aria-label="Аналитический ответ" data-testid="presentation">
    <p class="text-sm text-slate-400">{{ appliedConditions }}</p>
    <p v-if="document.summary" class="text-sm text-slate-300">{{ document.summary }} <span class="text-xs text-slate-500">· Интерпретация модели</span></p>
    <div class="grid grid-cols-1 gap-4" :class="document.blocks.length > 1 ? 'md:grid-cols-2' : ''"><AsyncBlock v-for="block in document.blocks" :key="block.id" :block="{ ...block, title: block.title || document.title }" :dataset="document.datasets[block.dataset_id]!" @open-source="$emit('openSource', $event)" /></div>
  </section>
</template>
<script setup lang="ts">
import { defineAsyncComponent, computed } from 'vue'
import type { PresentationDocument } from '../types/presentation'
const AsyncBlock = defineAsyncComponent(() => import('./PresentationBlock.vue'))
defineEmits<{ openSource: [id: number] }>()
const props = defineProps<{ document: PresentationDocument }>()
const appliedConditions = computed(() => Object.values(props.document.datasets).map(ds => {
  const range = ds.normalized_query.date_range as { start: string; end_exclusive: string } | null
  return `${range ? `Период: ${range.start} — ${range.end_exclusive} (не включая)` : 'Текущий срез'} · ${ds.normalized_query.currency || ''} · ${ds.timezone}${ds.effective_end_exclusive && range && ds.effective_end_exclusive !== range.end_exclusive ? ` · Факт до ${ds.effective_end_exclusive} (не включая)` : ''}`
}).filter((value, index, all) => all.indexOf(value) === index).join('; '))
</script>
