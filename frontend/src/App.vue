<template>
  <div class="relative min-h-screen w-full bg-[#060912] text-slate-100 flex flex-col justify-between selection:bg-indigo-500/30 selection:text-indigo-200 overflow-x-hidden">
    <!-- Interactive Canvas 2D Wave Background -->
    <WaveBackground />

    <!-- Top Navigation Header -->
    <AppHeader
      @go-home="goHome"
      @open-auth="isAuthModalOpen = true"
      @open-profile="handleOpenProfile"
    />

    <nav v-if="isAuthenticated" class="relative z-30 max-w-6xl mx-auto w-full px-4 flex flex-wrap gap-2" aria-label="Главная навигация">
      <button v-if="!currentUser?.roles.includes('client')" class="btn" @click="currentView = 'dashboard'; fetchKpiData()">KPI и чат</button>
      <button class="btn" @click="currentView = 'workspace'">Рабочий кабинет</button>
      <button class="btn" @click="currentView = 'profile'">Настройки профиля</button>
    </nav>
    <!-- Main Content Area -->
    <main class="relative z-10 flex-1 flex flex-col justify-center">
      <!-- Transition between Welcome State, Profile State, and Dashboard State -->
      <transition
        mode="out-in"
        enter-active-class="transition duration-300 ease-out"
        enter-from-class="opacity-0 translate-y-2"
        enter-to-class="opacity-100 translate-y-0"
        leave-active-class="transition duration-200 ease-in"
        leave-from-class="opacity-100 translate-y-0"
        leave-to-class="opacity-0 -translate-y-2"
      >
        <!-- View 1: Welcome Screen -->
        <WelcomeView
          v-if="currentView === 'welcome' && !currentUser?.roles.includes('client')"
          :suggestions="welcomeSuggestions"
          :disabled="isGenerating"
          @select-prompt="handlePromptSubmit"
          @attach-file="handleAttach"
        />

        <!-- View 2: User Profile (Личный кабинет) - Only for authenticated users -->
        <UserProfileView
          v-else-if="currentView === 'profile' && isAuthenticated"
          @back-to-chat="currentView = 'dashboard'"
          @logged-out="handleLogout"
        />

        <WorkspaceView v-else-if="isAuthenticated && (currentView === 'workspace' || currentUser?.roles.includes('client'))" :key="currentUser?.id" />
        <!-- View 3: KPI Dashboard Active Chat View -->
        <div v-else class="flex-1 flex flex-col justify-between py-2">
          <KpiDashboardView
            :data="kpiData"
            :widget="activeWidget"
            :presentation="activePresentation"
            :has-chat-response="hasChatResponse"
            :response-text="chatResponseText"
            :is-loading="isGenerating"
            :period="selectedPeriod"
            @open-source="openQuote"
            @change-period="fetchKpiData"
            @change-filters="changeKpiFilters"
            @select-prompt="handlePromptSubmit"
          />

          <div class="max-w-6xl mx-auto w-full px-4 space-y-3">
            <p v-if="error" role="alert" class="panel text-rose-300">{{ error }}</p>
            <button v-if="isGenerating" class="btn" @click="cancel">Отменить запрос</button>
            <dialog ref="sourceDialog" class="panel max-w-2xl backdrop:bg-black/70"><button class="btn mb-4" @click="sourceDialog?.close()">Закрыть источник</button><pre class="whitespace-pre-wrap">{{ sourceText }}</pre></dialog>
            <blockquote v-for="quote in quotes" :key="quote.id" class="panel text-sm"><p>{{ quote.content }}</p><p class="text-xs text-slate-400">{{ quote.sender_name }} · {{ quote.sent_at }} · <button class="underline" @click="openQuote(quote.id)">Открыть источник #{{ quote.id }}</button></p></blockquote>
          </div>
          <!-- Bottom Docked Chat Input Bar for Dashboard View -->
          <div class="w-full pb-6 pt-4 mt-auto">
            <ChatInput
              :suggestions="dashboardSuggestions"
              placeholder="Спросите Mazory..."
              :disabled="isGenerating"
              @submit="handlePromptSubmit"
              @attach-file="handleAttach"
            />
          </div>
        </div>
      </transition>
    </main>

    <!-- Phone / SMS Auth Modal -->
    <AuthModal
      :is-open="isAuthModalOpen"
      @close="isAuthModalOpen = false"
      @success="handleAuthSuccess"
    />

    <!-- Global Subtle Toast for Attachment Interactions -->
    <transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-4"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-4"
    >
      <div
        v-if="toastMessage"
        class="fixed bottom-24 right-6 z-50 px-4 py-2 rounded-xl bg-slate-900/90 border border-indigo-500/50 text-xs text-indigo-200 shadow-xl backdrop-blur-md"
      >
        {{ toastMessage }}
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import WorkspaceView from './components/WorkspaceView.vue'
import WaveBackground from './components/WaveBackground.vue'
import AppHeader from './components/AppHeader.vue'
import WelcomeView from './components/WelcomeView.vue'
import KpiDashboardView from './components/KpiDashboardView.vue'
import ChatInput from './components/ChatInput.vue'
import AuthModal from './components/AuthModal.vue'
import UserProfileView from './components/UserProfileView.vue'
import { api } from './composables/api'
import type { Source } from './types/platform'
import { useChat } from './composables/useChat'
import { useAuth } from './composables/useAuth'

const {
  currentView,
  isGenerating,
  welcomeSuggestions,
  dashboardSuggestions,
  selectedPeriod,
  kpiData,
  activeWidget, activePresentation, hasChatResponse,
  chatResponseText,
  fetchKpiData,
  handlePromptSubmit,
  goHome, error, quotes, cancel, changeKpiFilters
} = useChat()

const { isAuthenticated, currentUser, checkAuth, isAuthModalOpen } = useAuth()
watch(isAuthenticated, value => { if (!value) currentView.value = 'welcome' })
const sourceDialog = ref<HTMLDialogElement>()
const sourceText = ref('')
async function openQuote(id: number) {
  try { const source = await api<Source>(`/messages/${id}/`); sourceText.value = `${source.sender_name} · ${source.sent_at || 'Время неизвестно'}\n\n${source.content}`; sourceDialog.value?.showModal() }
  catch (e) { showToast(e instanceof Error ? e.message : 'Источник недоступен') }
}
const toastMessage = ref('')
let toastTimer: number | null = null

onMounted(() => {
  void checkAuth().then(ok => { if (ok) fetchKpiData() })
})

function showToast(msg: string) {
  toastMessage.value = msg
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => {
    toastMessage.value = ''
  }, 2500)
}

function handleOpenProfile() {
  if (isAuthenticated.value) {
    currentView.value = 'profile'
  } else {
    isAuthModalOpen.value = true
  }
}

function handleAuthSuccess() {
  showToast('✓ Вы успешно вошли в систему')
  fetchKpiData()
}

function handleLogout() {
  currentView.value = 'welcome'
  showToast('Вы вышли из системы')
}

function handleAttach() {
  if (!isAuthenticated.value) {
    isAuthModalOpen.value = true
    showToast('Для прикрепления файлов необходимо войти в систему')
    return
  }
  currentView.value = 'workspace'
  showToast('Откройте раздел «Документы» и выберите файл и проект')
}


</script>
