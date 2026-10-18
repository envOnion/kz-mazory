<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, post, download } from '../composables/api'
import { loadDirectory } from '../composables/directory'
import { currentUser } from '../composables/session'
import type { Directory, DirectoryProject } from '../types/platform'
import type { AttachmentItem } from '../types/finance'
import { ErrorState, StatusBadge } from './bits'
const items = ref<AttachmentItem[]>([]), directory = ref<Directory | null>(null)
const projectId = ref<number | null>(null), file = ref<File | null>(null)
const projectSearch = ref('')
const projectPage = ref(1)
const projectCache = ref<DirectoryProject[]>([])
const error = ref(''), notice = ref(''), busy = ref(false), transcript = ref('')
const client = computed(() => currentUser.value?.roles.includes('client'))
const canPublish = computed(() => currentUser.value?.roles.some(role => role === 'team_lead' || role === 'admin'))
const matchingProjects = computed(() => {
  const current = directory.value?.projects ?? []
  const selected = projectCache.value.find(project => project.id === projectId.value)
  return selected && !current.some(project => project.id === selected.id) ? [...current, selected] : current
})
async function loadProjects() {
  try {
    directory.value = await loadDirectory({ collections: [], includeProjects: true, projectPage: projectPage.value, projectSearch: projectSearch.value })
    const cache = new Map<number, DirectoryProject>()
    for (const project of projectCache.value) cache.set(project.id, project)
    for (const project of directory.value.projects) cache.set(project.id, project)
    projectCache.value = [...cache.values()]
  } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось загрузить проекты' }
}
async function load() {
  error.value = ''
  try {
    items.value = await api<AttachmentItem[]>('/attachments/')
    if (!client.value) await loadProjects()
  } catch (e) { error.value = e instanceof Error ? e.message : 'Вложения недоступны' }
}
async function searchProjects() { projectPage.value = 1; await loadProjects() }
async function changeProjectPage(delta: number) { projectPage.value += delta; await loadProjects() }
async function upload() {
  if (!file.value || !projectId.value) return
  busy.value = true; error.value = ''
  const form = new FormData(); form.set('project_id', String(projectId.value)); form.set('file',file.value)
  try { await api('/attachments/',{method:'POST',body:form}); notice.value = 'Файл принят в фоновую обработку'; await load() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось загрузить файл' }
  finally { busy.value = false }
}
async function open(item: AttachmentItem) {
  try { const result = await api<{transcript:string}>(`/attachments/${item.id}/`); transcript.value = result.transcript || 'Извлечённого текста пока нет' }
  catch (e) { error.value = e instanceof Error ? e.message : 'Вложение недоступно' }
}
async function publish(item: AttachmentItem) {
  try { await post(`/attachments/${item.id}/`,{published_to_client:!item.published_to_client}); await load() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось изменить публикацию' }
}
async function getFile(item: AttachmentItem) {
  try { await download(`/attachments/${item.id}/?download=1`,`document-${item.id}`) }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось скачать' }
}
function choose(event:Event) { file.value = (event.target as HTMLInputElement).files?.[0] ?? null }
onMounted(load)
</script>
<template>
  <div class="p-4 space-y-4"><h2 class="text-lg font-semibold">Вложения проектов</h2><ErrorState v-if="error" :text="error" :on-retry="load" /><p v-if="notice" role="status" class="text-emerald-300">{{ notice }}</p>
    <form v-if="!client" class="mx-card p-4 grid gap-3" @submit.prevent="upload"><div class="flex items-end gap-2"><label class="flex-1">Поиск проекта<input v-model="projectSearch" class="mx-input block mt-1 w-full" placeholder="Название проекта или компании" /></label><button type="button" class="mx-btn" @click="searchProjects">Найти</button></div><p v-if="directory" class="mx-muted">Проектов найдено: {{ directory.projects_count ?? directory.projects.length }} · Страница {{ projectPage }}</p><div v-if="directory" class="flex gap-2"><button type="button" class="mx-btn" :disabled="projectPage === 1" @click="changeProjectPage(-1)">Предыдущая</button><button type="button" class="mx-btn" :disabled="!directory.projects_next_page" @click="changeProjectPage(1)">Следующая</button></div><label>Проект<select aria-label="Проект вложения" v-model="projectId" class="mx-select" required><option v-for="p in matchingProjects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label><label>Документ, изображение или аудио (до 10 МБ)<input type="file" accept="image/png,image/jpeg,application/pdf,audio/ogg,audio/wav,audio/mpeg" class="block mt-2" required @change="choose" /></label><button class="mx-btn justify-self-start" :disabled="busy || !file || !projectId">Загрузить вложение</button></form>
    <article v-for="item in items" :key="item.id" :data-attachment-id="item.id" class="mx-card p-4 space-y-2"><div class="flex items-center gap-2 flex-wrap"><span>{{ directory?.projects.find(p => p.id === item.project_id)?.name ?? `Проект #${item.project_id}` }}</span><StatusBadge :state="item.state" /><span class="mx-muted">{{ item.content_type }}</span></div><div class="flex gap-2 flex-wrap"><button class="mx-btn" @click="getFile(item)">Скачать</button><button class="mx-btn" @click="open(item)">Извлечённый текст</button><button v-if="canPublish" class="mx-btn" @click="publish(item)">{{ item.published_to_client ? 'Снять публикацию для клиента' : 'Опубликовать клиенту' }}</button></div></article>
    <p v-if="!items.length" class="mx-muted">Доступных вложений нет</p><pre v-if="transcript" class="mx-card p-4 whitespace-pre-wrap break-words">{{ transcript }}</pre><button class="mx-btn" @click="load">Обновить состояние</button>
  </div>
</template>
