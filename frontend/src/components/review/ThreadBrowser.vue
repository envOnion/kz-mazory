<template>
  <section class="space-y-4">
    <p class="text-slate-400">Незавершённые мысли хранятся здесь. В проверку фактов попадают предложения с достаточными доказательствами.</p>
    <section v-if="lead && chats.length" class="panel space-y-2">
      <label>История чата <select v-model="chatId" class="field"><option v-for="chat in chats" :key="chat.id" :value="chat.id">{{ chat.name }}</option></select></label>
      <button class="btn" :disabled="loading || !chatId" @click="rebuild">Разобрать историю по темам</button>
      <p v-if="notice" role="status">{{ notice }}</p>
    </section>
    <form class="flex gap-2" @submit.prevent="load(1)"><input v-model="search" class="field flex-1" placeholder="Найти тему переписки" /><button class="btn" :disabled="loading">Найти</button></form>
    <p v-if="error" role="alert" class="text-rose-300">{{ error }}</p>
    <p v-if="loading" role="status">Загрузка тем…</p>
    <article v-for="item in items" :key="item.id" class="panel space-y-2">
      <button class="text-indigo-300 text-left" @click="open(item.id)">Тема #{{ item.id }}: {{ item.topic }}</button>
      <p class="text-sm">{{ labels[item.state] }} · Версия {{ item.version }}</p>
      <template v-if="detail?.id === item.id">
        <p>{{ detail.summary }}</p>
        <p v-if="detail.parent_id" class="text-xs">Подтема треда #{{ detail.parent_id }}</p>
        <article v-for="message in detail.messages" :key="message.id" class="border-l-2 border-indigo-400 pl-3 whitespace-pre-wrap text-sm">
          <strong>{{ message.sender_name }}</strong> · {{ states[message.thought_state] }}<p>{{ message.content }}</p>
        </article>
      </template>
    </article>
    <p v-if="!items.length && !loading">Тематических тредов пока нет.</p>
    <nav class="flex gap-2" aria-label="Страницы тем"><button class="btn" :disabled="loading || page === 1" @click="load(page - 1)">Ранее</button><button class="btn" :disabled="loading || !next" @click="load(page + 1)">Далее</button></nav>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, post } from '../../composables/api'
import { currentUser } from '../../composables/session'
import type { Page, Directory } from '../../types/platform'
import type { DialogueThread } from '../../types/factReview'
const chats = ref<NonNullable<Directory['chats']>>([]), chatId = ref<number | null>(null), notice = ref('')
const lead = computed(() => currentUser.value?.roles.includes('team_lead'))
const search = ref(''), page = ref(1), next = ref(false), loading = ref(false), error = ref('')
const items = ref<Pick<DialogueThread, 'id' | 'topic' | 'state' | 'version'>[]>([]), detail = ref<DialogueThread | null>(null)
const labels = { open: 'Мысль продолжается', ready: 'Мысль определена', unknown: 'Требует уточнения', superseded: 'Заменена' }
const states = { intermediate: 'Промежуточная реплика', final: 'Завершает мысль', unknown: 'Связь неизвестна' }
async function load(number: number) {
  loading.value = true; error.value = ''
  try { const result = await api<Page<Pick<DialogueThread, 'id' | 'topic' | 'state' | 'version'>>>(`/threads/?page=${number}&search=${encodeURIComponent(search.value)}`); items.value = result.results; next.value = Boolean(result.next); page.value = number; detail.value = null }
  catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось загрузить темы' }
  finally { loading.value = false }
}
async function open(id: number) { try { detail.value = await api<DialogueThread>(`/threads/${id}/`) } catch (e) { error.value = e instanceof Error ? e.message : 'Тема недоступна' } }
async function rebuild() { try { await post('/threads/backfill/', { config_id: chatId.value, request_key: crypto.randomUUID() }); notice.value = 'Разбор истории поставлен в очередь. Обновите список тем после обработки.' } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось запустить разбор' } }
onMounted(async () => { await load(1); try { const directory = await api<Directory>('/directory/'); chats.value = directory.chats || []; chatId.value = chats.value[0]?.id || null } catch (e) { error.value = e instanceof Error ? e.message : 'Не удалось загрузить чаты' } })
</script>
