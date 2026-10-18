<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { Search, X } from 'lucide-vue-next'
const props=defineProps<{sections:{path:string;label:string}[]}>()
const open=ref(false), query=ref(''), input=ref<HTMLInputElement|null>(null), trigger=ref<HTMLButtonElement|null>(null)
const results=computed(()=>{const q=query.value.trim().toLocaleLowerCase('ru'); return props.sections.map((row,index)=>({...row,index,score:row.label.toLocaleLowerCase('ru')===q ? 0 : 1})).filter(row=>row.label.toLocaleLowerCase('ru').includes(q)).sort((a,b)=>a.score-b.score || a.index-b.index)})
async function show(){open.value=true; query.value=''; await nextTick(); input.value?.focus()}
function close(){open.value=false; trigger.value?.focus()}
function keydown(e:KeyboardEvent){if(e.key==='Escape'){e.stopPropagation();close()} if(e.key==='Tab'){const buttons=Array.from((e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('input,button,a')); const first=buttons[0],last=buttons.at(-1);if(e.shiftKey && document.activeElement===first){e.preventDefault();last?.focus()} else if(!e.shiftKey && document.activeElement===last){e.preventDefault();first?.focus()}}}
</script>
<template>
  <button ref="trigger" class="mx-btn !p-2 mb-2" aria-label="Найти раздел" @click="show"><Search :size="18" /></button>
  <Teleport to="body"><template v-if="open"><div class="mx-overlay !z-50" @click="close"/><section role="dialog" aria-modal="true" aria-label="Поиск раздела" class="mx-card fixed z-[60] p-4 w-[calc(100%-32px)] max-w-md left-1/2 -translate-x-1/2 top-12 space-y-3" style="color:var(--mx-text)" @keydown="keydown"><header class="flex gap-2"><input ref="input" v-model="query" class="mx-input" aria-label="Название раздела" placeholder="Найти раздел…"/><button class="mx-btn !p-2" aria-label="Закрыть поиск" @click="close"><X :size="18"/></button></header><nav class="flex flex-col gap-1" aria-label="Результаты поиска разделов"><RouterLink v-for="row in results" :key="row.path" :to="row.path" class="mx-tab text-left" @click="close">{{ row.label }}</RouterLink></nav><p v-if="!results.length" class="mx-muted">Подходящих разделов нет</p></section></template></Teleport>
</template>
