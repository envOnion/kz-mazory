<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AuthModal from './components/AuthModal.vue'
import NavigationRail from './shell/NavigationRail.vue'
import { useAuth } from './composables/useAuth'
import { api } from './composables/api'
import { currentUser } from './composables/session'
import { isClient, defaultPath } from './router'
import type { DialogSummary } from './types/messenger'

// Единая оболочка Mazory: слева навигационная полоса, справа — раздел.
// Без fallback: при отсутствии сессии — только вход; демоданных нет.
const { isAuthenticated, isAuthModalOpen, checkAuth } = useAuth()
const route = useRoute()
const router = useRouter()
const booted = ref(false)
const attention = ref(0)
const attentionStale = ref(false)
let attentionTimer: number | null = null
let attentionBusy = false

const staff = computed(() => Boolean(currentUser.value) && !isClient())

async function refreshAttention() {
  if (!staff.value) { attention.value = 0; attentionStale.value = false; return }
  if (attentionBusy) return
  attentionBusy = true
  try {
    const page = await api<{ items: DialogSummary[] }>('/messenger/dialogs/?attention=1', {signal: AbortSignal.timeout(20000)})
    attention.value = page.items.reduce((total, dialog) => total + dialog.attention_count, 0)
    attentionStale.value = false
  } catch { attentionStale.value = true }
  finally { attentionBusy = false }
}

onMounted(async () => {
  if (!isAuthenticated.value) await checkAuth()
  await router.isReady()
  booted.value = true
  await refreshAttention()
  attentionTimer = window.setInterval(refreshAttention, 30000)
})
onBeforeUnmount(() => { if (attentionTimer) clearInterval(attentionTimer) })
watch(() => route.path, refreshAttention)
watch(isAuthenticated, value => {
  if (value) { void refreshAttention(); if (booted.value && route.path === '/') void router.replace(defaultPath()) }
  else if (booted.value) void router.replace('/')
})
</script>

<template>
  <div v-if="!booted" class="mx-app items-center justify-center">
    <div class="mx-skeleton" style="width:180px;height:18px" />
  </div>

  <div v-else-if="!currentUser" class="mx-app" style="flex-direction:column;align-items:center;justify-content:center;gap:18px;text-align:center;padding:24px">
    <div class="mx-accent" style="font-size:34px;font-weight:700">Mazory</div>
    <p class="mx-muted" style="max-width:380px;font-size:14px">
      Рабочее пространство операций: чаты, аналитика, очередь обработки и изменения CRM в одном интерфейсе.
    </p>
    <button class="mx-btn mx-btn-primary" data-testid="open-auth" @click="isAuthModalOpen = true">Войти по номеру телефона</button>
    <AuthModal :is-open="isAuthModalOpen" @close="isAuthModalOpen = false" @success="refreshAttention" />
  </div>

  <div v-else-if="staff" class="mx-app">
    <NavigationRail :attention="attention" :attention-stale="attentionStale" />
    <main data-main-region tabindex="-1" class="flex-1 min-w-0 flex overflow-hidden outline-none">
      <RouterView />
    </main>
    <AuthModal :is-open="isAuthModalOpen" @close="isAuthModalOpen = false" @success="refreshAttention" />
  </div>

  <div v-else class="mx-app">
    <main data-main-region tabindex="-1" class="flex-1 min-w-0 flex overflow-hidden outline-none">
      <RouterView />
    </main>
  </div>
</template>
