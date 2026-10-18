<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../composables/api'
import type { Paged, TraceContextItem } from '../types/messenger'
import { ErrorState, formatTime } from './bits'
const props = defineProps<{recordId:number;traceId:number}>()
const open = ref(false), rows = ref<TraceContextItem[]>([]), cursor = ref<string|null>(null), error = ref(''), busy = ref(false)
async function load(more=false) {
  busy.value = true; error.value = ''; open.value = true
  try { const result = await api<Paged<TraceContextItem>>(`/messenger/records/${props.recordId}/traces/${props.traceId}/context/${more && cursor.value ? '?cursor='+encodeURIComponent(cursor.value) : ''}`); rows.value = more ? [...rows.value,...result.items] : result.items; cursor.value = result.next_cursor }
  catch(e) { error.value = e instanceof Error ? e.message : 'Контекст недоступен' }
  finally { busy.value = false }
}
</script>
<template>
  <div class="mt-2"><button class="mx-link" :disabled="busy" @click="open ? open=false : load()">{{ open ? 'Скрыть контекст' : 'Показать использованный контекст' }}</button>
    <div v-if="open" class="mt-2 space-y-2"><ErrorState v-if="error" :text="error" :on-retry="() => load()" /><article v-for="(row,index) in rows" :key="index" class="mx-card p-3 text-xs"><p class="mx-muted">{{ row.sender }} · {{ formatTime(row.time) }} · #{{ row.raw_message_id }}</p><p class="whitespace-pre-wrap mt-1">{{ row.body }}</p><p v-if="row.partial" class="text-amber-200">В этой части показан фрагмент сообщения</p><RouterLink v-if="row.href" :to="row.href" class="mx-link">Открыть исходное сообщение →</RouterLink></article><p v-if="!rows.length && !busy && !error" class="mx-muted">Доступного сохранённого контекста нет</p><button v-if="cursor" class="mx-btn" :disabled="busy" @click="load(true)">Следующая часть контекста</button></div>
  </div>
</template>
