<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, nextTick } from 'vue'
import { Bell, X } from 'lucide-vue-next'
import { api, post } from '../composables/api'
import type { NotificationItem } from '../types/platform'
import { ErrorState, StatusBadge, formatTime } from './bits'
const items = ref<NotificationItem[]>([]), unread = ref(0), open = ref(false), error = ref('')
const trigger = ref<HTMLButtonElement | null>(null), panel = ref<HTMLElement | null>(null)
async function load() {
  try { const data = await api<{notifications:NotificationItem[];unread_count:number}>('/notifications/'); items.value = data.notifications; unread.value = data.unread_count; error.value = '' }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось получить уведомления' }
}
async function toggle() { open.value = !open.value; if (open.value) { await load(); await nextTick(); panel.value?.focus() } }
function close() { open.value = false; trigger.value?.focus() }
async function readAll() { try { await post('/notifications/read-all/',{}); await load() } catch(e) { error.value = e instanceof Error ? e.message : 'Не удалось отметить уведомления' } }
function keydown(event:KeyboardEvent) {
  if(!open.value) return
  if(event.key === 'Escape'){event.stopPropagation();close()}
  if(event.key === 'Tab') {
    const elements=Array.from(panel.value?.querySelectorAll<HTMLElement>('button,a,input,[tabindex="0"]') ?? []).filter(el=>!el.hasAttribute('disabled')&&el.getClientRects().length)
    const first=elements[0],last=elements.at(-1)
    if(event.shiftKey&&(document.activeElement===first||document.activeElement===panel.value)){event.preventDefault();last?.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
  }
}
let timer:number | null = null
onMounted(() => { void load(); timer = window.setInterval(() => { if (open.value && !document.hidden) void load() },5000); document.addEventListener('keydown',keydown) })
onBeforeUnmount(() => { if (timer) clearInterval(timer); document.removeEventListener('keydown',keydown) })
const clarificationText = (state:string) => state === 'resolved' ? 'Ответ применён, запись в CRM проверена' : state === 'unconfirmed' ? 'Оставлено без подтверждения' : state === 'answered' ? 'Ответ принят, выполняется проверка факта' : ['expired','superseded'].includes(state) ? 'Вопрос устарел, запись удержана' : 'Ожидается ответ в WhatsApp'
</script>
<template>
  <button ref="trigger" title="Уведомления" class="relative flex flex-col items-center gap-1 py-2 w-full mx-muted" :aria-expanded="open" @click="toggle"><Bell :size="19" /><span class="text-[9px]">Уведомления</span><span v-if="unread" class="absolute right-1 top-0 mx-chip mx-chip-warn">{{ unread }}</span></button>
  <Teleport to="body"><template v-if="open"><div class="mx-overlay !z-50" @click="close" /><section ref="panel" tabindex="-1" role="dialog" aria-modal="true" aria-label="Персональные уведомления" class="mx-card fixed z-[60] p-4 shadow-xl flex flex-col gap-3" style="left:calc(var(--mx-rail-w) + 6px);bottom:24px;width:min(380px,calc(100vw - var(--mx-rail-w) - 12px));max-height:80dvh;color:var(--mx-text)">
    <header class="flex gap-2 items-center"><h2 class="font-semibold flex-1">Персональные уведомления</h2><button class="mx-btn !p-2" aria-label="Закрыть уведомления" @click="close"><X :size="16" /></button></header>
    <button v-if="unread" class="mx-link text-left" @click="readAll">Прочитать все</button><ErrorState v-if="error" :text="error" :on-retry="load" />
    <div class="overflow-y-auto space-y-3"><article v-for="item in items" :key="item.id" class="mx-card p-3 space-y-2 text-sm"><h3 class="font-semibold">{{ item.title }}</h3><p class="whitespace-pre-wrap">{{ item.message }}</p><p class="mx-muted text-xs">{{ formatTime(item.created_at) }}</p><div v-for="(delivery,index) in item.deliveries" :key="index"><StatusBadge :state="delivery.state" /></div><p v-if="item.clarification" class="text-xs text-violet-200">{{ clarificationText(item.clarification.state) }}</p></article><p v-if="!items.length && !error" class="mx-muted">Уведомлений нет</p></div>
  </section></template></Teleport>
</template>
