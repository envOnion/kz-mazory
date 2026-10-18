<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../composables/api'
import { ErrorState, Skeleton, formatTime } from './bits'
import type { DiagnosticsPage } from '../types/settings'
const route=useRoute(),router=useRouter(),data=ref<DiagnosticsPage|null>(null),error=ref(''),busy=ref(false),objectId=ref('')
const resource=computed(()=>typeof route.query.resource==='string' ? route.query.resource : 'audit')
let pending:AbortController|null=null
async function load(more=false){
  pending?.abort();const request=new AbortController();pending=request;busy.value=true;error.value=''
  try {
    const params=new URLSearchParams({resource:resource.value})
    if(typeof route.query.object==='string')params.set('object',route.query.object)
    if(more&&data.value?.next_cursor)params.set('cursor',data.value.next_cursor)
    const result=await api<DiagnosticsPage>(`/settings/diagnostics/?${params}`,{signal:request.signal})
    if(!request.signal.aborted)data.value=more&&data.value ? {...result,items:[...data.value.items,...result.items]} : result
  }catch(e){if(!request.signal.aborted)error.value=e instanceof Error ? e.message : 'Диагностика недоступна'}
  finally{if(!request.signal.aborted)busy.value=false}
}
function select(id:string){void router.push({path:'/settings',query:{section:'system',resource:id}})}
function filter(){void router.push({path:'/settings',query:{section:'system',resource:resource.value,object:objectId.value.trim()||undefined}})}
function cell(value:unknown){return value===null||value===undefined ? '—' : typeof value==='object' ? JSON.stringify(value,null,2) : typeof value==='boolean' ? value ? 'Да' : 'Нет' : String(value)}
watch(()=>[route.query.resource,route.query.object],()=>{data.value=null;objectId.value=typeof route.query.object==='string' ? route.query.object : '';void load()})
onMounted(()=>{objectId.value=typeof route.query.object==='string' ? route.query.object : '';void load()})
onBeforeUnmount(()=>pending?.abort())
</script>
<template>
  <section aria-label="Служебная диагностика" class="space-y-3">
    <nav v-if="data" aria-label="Диагностика системы" class="flex flex-wrap gap-2"><button v-for="item in data.resources" :key="item.id" class="mx-tab" :class="resource===item.id ? 'mx-tab-active' : ''" @click="select(item.id)">{{ item.label }}</button></nav>
    <ErrorState v-if="error" :text="error" :on-retry="()=>load()"/><Skeleton v-if="busy&&!data"/>
    <template v-if="data"><h2 class="font-semibold text-lg">{{ data.title }}</h2><p class="mx-muted">{{ data.description }}</p><p class="mx-muted text-xs">Состояние на {{ formatTime(data.as_of) }}</p>
      <form v-if="resource!=='coverage'" class="flex flex-wrap gap-2" @submit.prevent="filter"><label class="grow">Точный ID записи<input v-model="objectId" class="mx-input" maxlength="64"/></label><button class="mx-btn self-end" :disabled="busy">Найти запись</button><button class="mx-btn self-end" type="button" @click="select(resource)">Все записи</button></form>
      <p v-if="!data.items.length&&!busy" class="mx-muted">Записей по текущему фильтру нет</p>
      <div class="overflow-x-auto"><table class="mx-table" data-testid="diagnostic-table"><thead><tr><th v-for="column in data.columns" :key="column.key">{{ column.label }}</th></tr></thead><tbody><tr v-for="(row,index) in data.items" :key="index"><td v-for="column in data.columns" :key="column.key"><pre class="whitespace-pre-wrap break-words max-h-80 overflow-y-auto min-w-20 max-w-lg font-sans">{{ cell(row[column.key]) }}</pre></td></tr></tbody></table></div>
      <button v-if="data.next_cursor" class="mx-btn" :disabled="busy" @click="load(true)">Следующие записи</button><button class="mx-btn ml-2" :disabled="busy" @click="load()">Обновить диагностику</button>
    </template>
  </section>
</template>
