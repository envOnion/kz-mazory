<template>
  <section class="panel space-y-4" aria-label="MCP проверки фактов">
    <h3 class="font-semibold">MCP проверки фактов</h3>
    <p class="text-sm text-slate-400">Подключите внешний AI для просмотра контекста и проверки фактов. Решения требуют основания и ваших прав доступа.</p>
    <label class="block text-sm">Адрес сервера
      <input class="w-full rounded-lg bg-slate-950 border border-slate-700 p-2 mt-1" readonly :value="endpoint" />
    </label>
    <p class="text-sm">Транспорт: Streamable HTTP · Заголовок: <code>Authorization: Bearer &lt;токен&gt;</code></p>
    <p v-if="error" role="alert" class="text-rose-300">{{ error }}</p>
    <p v-if="notice" role="status" class="text-emerald-300">{{ notice }}</p>
    <p v-if="loading" class="text-slate-400">Загрузка подключения…</p>
    <template v-else-if="connection">
      <label class="block text-sm">Персональный токен
        <input class="w-full rounded-lg bg-slate-950 border border-slate-700 p-2 mt-1" readonly autocomplete="off" :type="revealed ? 'text' : 'password'" :value="connection.token" />
      </label>
      <div class="flex flex-wrap gap-2">
        <button class="btn" @click="revealed = !revealed">{{ revealed ? 'Скрыть токен' : 'Показать токен' }}</button>
        <button class="btn" @click="copyToken">Скопировать токен</button>
        <button class="btn" :disabled="busy" @click="change('rotate')">Перевыпустить токен</button>
        <button class="btn" :disabled="busy" @click="change('revoke')">Отозвать токен</button>
      </div>
      <p class="text-xs text-slate-400">Создан: {{ date(connection.created_at) }} · Использован: {{ date(connection.last_used_at) }}<template v-if="connection.expires_at"> · Действует до: {{ date(connection.expires_at) }}</template></p>
      <p class="text-xs text-slate-400">При перевыпуске или отзыве прежний токен перестаёт работать.</p>
    </template>
    <div v-else class="space-y-2">
      <p class="text-sm text-slate-400">Активного токена нет. Его можно получить здесь или при следующем входе по телефону.</p>
      <button class="btn" :disabled="busy" @click="change('create')">Получить токен MCP</button>
      <button v-if="error" class="btn" :disabled="busy" @click="change('rotate')">Перевыпустить токен</button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { api, post } from '../../composables/api'
import { API_BASE } from '../../composables/session'
import type { McpConnection, McpConnectionResponse, McpConnectionAction } from '../../types/mcp'

const connection = ref<McpConnection | null>(null)
const loading = ref(true), busy = ref(false), revealed = ref(false), error = ref(''), notice = ref('')
const endpoint = new URL(`${API_BASE}/mcp/fact-review/`, window.location.origin).href
let disposed = false
const date = (value: string | null) => value ? new Date(value).toLocaleString() : 'ещё не использован'

async function load() {
  try {
    const result = await api<McpConnectionResponse>('/mcp/connection/', { cache: 'no-store' })
    if (!disposed) connection.value = result.connection
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : 'Не удалось загрузить подключение MCP' }
  finally { loading.value = false }
}
async function change(action: McpConnectionAction) {
  busy.value = true; error.value = ''; notice.value = ''; revealed.value = false
  try {
    const result = await post<McpConnectionResponse>('/mcp/connection/', { action })
    if (!disposed) connection.value = result.connection
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : 'Не удалось обновить токен' }
  finally { busy.value = false }
}
async function copyToken() {
  if (!connection.value) return
  try { await navigator.clipboard.writeText(connection.value.token); notice.value = 'Токен скопирован' }
  catch { error.value = 'Не удалось скопировать. Покажите токен и скопируйте его вручную.' }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; connection.value = null })
</script>
