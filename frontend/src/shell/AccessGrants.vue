<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, post } from '../composables/api'
import { ErrorState } from './bits'
import type { AccessGrantsData } from '../types/settings'
import SettingsForm from './SettingsForm.vue'
const data = ref<AccessGrantsData | null>(null), error = ref(''), notice = ref(''), busy = ref(false)
const form = ref({kind:'chat',name:'',user_id:null as number|null,config_id:null as number|null,project_id:null as number|null,enabled:true,status:'active'})
async function load() { try { data.value = await api<AccessGrantsData>('/settings/access/grants/') } catch(e) { error.value = e instanceof Error ? e.message : 'Ошибка доступа' } }
async function save() {
  busy.value = true; error.value = ''; notice.value = ''
  const {kind,name,user_id,config_id,project_id,enabled,status} = form.value
  try { await post('/settings/access/grants/', kind === 'team' ? {kind,name,enabled} : kind === 'chat' ? {kind,user_id,config_id,enabled} : {kind,user_id,project_id,status}); await load(); notice.value = 'Доступ сохранён' }
  catch(e) { error.value = e instanceof Error ? e.message : 'Не удалось сохранить доступ' }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <div class="space-y-4"><ErrorState v-if="error" :text="error" :on-retry="load" /><p v-if="notice" role="status" class="text-emerald-300">{{ notice }}</p>
    <details v-for="team in data?.teams" :key="team.id" class="mx-card p-4"><summary class="cursor-pointer">Параметры команды · {{ team.name }}</summary><SettingsForm class="mt-3" :row="team" :fields="data?.team_fields ?? []" :endpoint="`/settings/access/grants/?team=${team.id}`" @saved="load" /></details>
    <form class="mx-card p-4 grid gap-3" @submit.prevent="save"><h2 class="font-semibold">Команды и специальные доступы</h2><label>Действие<select v-model="form.kind" class="mx-select"><option value="chat">Доступ к чату</option><option value="client">Доступ клиента к проекту</option><option value="team">Создать команду</option></select></label>
      <label v-if="form.kind === 'team'">Название команды<input v-model="form.name" class="mx-input" required /></label>
      <label v-else>Пользователь<select v-model="form.user_id" class="mx-select" required><option v-for="user in data?.users" :key="user.id" :value="user.id">{{ user.first_name || user.username }} {{ user.last_name }}</option></select></label>
      <label v-if="form.kind === 'chat'">Чат<select v-model="form.config_id" class="mx-select" required><option v-for="config in data?.configs" :key="config.id" :value="config.id">{{ config.name }}</option></select></label>
      <label v-if="form.kind === 'client'">Проект<select v-model="form.project_id" class="mx-select" required><option v-for="project in data?.projects" :key="project.id" :value="project.id">{{ project.name }}</option></select></label>
      <label v-if="form.kind === 'client'">Статус<select v-model="form.status" class="mx-select"><option value="active">Активен</option><option value="invited">Приглашён</option><option value="revoked">Отозван</option></select></label>
      <label v-else><input v-model="form.enabled" type="checkbox" /> Активно</label><button class="mx-btn justify-self-start" :disabled="busy">Сохранить</button>
    </form>
    <div v-if="data" class="mx-card p-4 space-y-3"><h3 class="font-semibold">Текущие доступы</h3><div v-for="grant in data.chats" :key="`chat-${grant.id}`">{{ data.users.find(u => u.id === grant.user_id)?.username }} → {{ data.configs.find(c => c.id === grant.config_id)?.name }} · {{ grant.is_active ? 'Активен' : 'Отозван' }}</div><div v-for="grant in data.clients" :key="`client-${grant.id}`">{{ data.users.find(u => u.id === grant.user_id)?.username }} → {{ data.projects.find(p => p.id === grant.project_id)?.name }} · {{ {active:'Активен',invited:'Приглашён',revoked:'Отозван'}[grant.status] || grant.status }}</div></div>
  </div>
</template>
