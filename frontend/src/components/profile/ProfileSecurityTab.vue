<template><div class="space-y-5"><h3 class="font-semibold">Активные сессии</h3><p class="text-sm text-slate-400">Доступ — 15 минут, обновление сессии — до 30 дней. Отозванная сессия перестаёт работать сразу.</p><p role="alert" v-if="error" class="text-rose-300">{{ error }}</p><article class="panel" v-for="session in sessions" :key="session.id"><p class="break-words text-sm">{{ session.device || 'Устройство не указано' }}</p><p class="text-xs text-slate-400 my-2">Создана: {{ new Date(session.created_at).toLocaleString() }} · Активность: {{ new Date(session.last_used_at).toLocaleString() }}</p><button class="btn" @click="revoke(session.id)">Завершить сессию #{{ session.id }}</button></article><button class="btn" @click="revokeAll">Завершить все сессии</button></div></template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api, post } from '../../composables/api'
import { clearSession } from '../../composables/session'
import type { Session } from '../../types/platform'
const sessions = ref<Session[]>([]), error = ref('')
async function load() { try { sessions.value = await api<Session[]>('/auth/sessions/') } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка' } }
async function revoke(id: number) { await post(`/auth/sessions/${id}/revoke/`, {}); await load() }
async function revokeAll() { await post('/auth/sessions/', {}); clearSession() }
onMounted(load)
</script>
