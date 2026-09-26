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
          v-if="currentView === 'welcome'"
          :suggestions="welcomeSuggestions"
          @select-prompt="handlePromptSubmit"
          @attach-file="handleAttach"
          @voice-input="handleVoice"
        />

        <!-- View 2: User Profile (Личный кабинет) - Only for authenticated users -->
        <UserProfileView
          v-else-if="currentView === 'profile' && isAuthenticated"
          @back-to-chat="currentView = 'dashboard'"
          @logged-out="handleLogout"
        />

        <!-- View 3: KPI Dashboard Active Chat View -->
        <div v-else class="flex-1 flex flex-col justify-between py-2">
          <KpiDashboardView
            :data="kpiData"
            :widget="activeWidget"
            :response-text="chatResponseText"
            :is-loading="isGenerating"
            @select-prompt="handlePromptSubmit"
          />

          <!-- Bottom Docked Chat Input Bar for Dashboard View -->
          <div class="w-full pb-6 pt-4 mt-auto">
            <ChatInput
              :suggestions="dashboardSuggestions"
              placeholder="Спросите Mazory..."
              :disabled="isGenerating"
              @submit="handlePromptSubmit"
              @attach-file="handleAttach"
              @voice-input="handleVoice"
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

    <!-- Global Subtle Toast for Attachment/Mic Interactions -->
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
import { ref, onMounted } from 'vue'
import WaveBackground from './components/WaveBackground.vue'
import AppHeader from './components/AppHeader.vue'
import WelcomeView from './components/WelcomeView.vue'
import KpiDashboardView from './components/KpiDashboardView.vue'
import ChatInput from './components/ChatInput.vue'
import AuthModal from './components/AuthModal.vue'
import UserProfileView from './components/UserProfileView.vue'
import { useChat } from './composables/useChat'
import { useAuth } from './composables/useAuth'

const {
  currentView,
  isGenerating,
  welcomeSuggestions,
  dashboardSuggestions,
  kpiData,
  activeWidget,
  chatResponseText,
  fetchKpiData,
  handlePromptSubmit,
  goHome
} = useChat()

const { isAuthenticated, checkAuth, isAuthModalOpen } = useAuth()
const toastMessage = ref('')
let toastTimer: number | null = null

onMounted(() => {
  checkAuth()
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
  showToast('Прикрепление файлов: выберите документ Excel, PDF или скриншот')
}

function handleVoice() {
  if (!isAuthenticated.value) {
    isAuthModalOpen.value = true
    showToast('Для голосового ввода необходимо войти в систему')
    return
  }
  showToast('Голосовой ввод активирован (слушаю...)')
}
</script>
