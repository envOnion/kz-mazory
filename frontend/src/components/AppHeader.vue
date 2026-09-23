<template>
  <header class="w-full flex items-center justify-between px-6 py-4 z-20 select-none">
    <!-- Brand logo + title -->
    <div
      class="flex items-center gap-3 cursor-pointer group"
      @click="emit('goHome')"
      title="На главный экран"
    >
      <MazoryLogo :size="28" :glow="true" />
      <span class="text-white text-lg font-semibold tracking-tight transition-colors group-hover:text-indigo-200">
        Mazory
      </span>
    </div>

    <!-- Right controls: notifications, settings, auth/avatar -->
    <div class="flex items-center gap-3.5">
      <!-- Notification bell with badge -->
      <button
        type="button"
        class="relative p-2 text-slate-300 hover:text-white rounded-full hover:bg-white/5 transition-colors focus:outline-none cursor-pointer"
        title="Уведомления"
        @click="emit('toggleNotifications')"
      >
        <Bell class="w-5 h-5" />
        <span class="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.8)]"></span>
      </button>

      <!-- Settings button -->
      <button
        type="button"
        class="p-2 text-slate-300 hover:text-white rounded-full hover:bg-white/5 transition-colors focus:outline-none cursor-pointer"
        title="Настройки"
      >
        <Settings class="w-5 h-5" />
      </button>

      <!-- User avatar / Auth Dropdown Trigger -->
      <div class="relative">
        <div
          @click="showDropdown = !showDropdown"
          class="flex items-center gap-2.5 p-1 rounded-full hover:bg-white/5 cursor-pointer transition-all border border-transparent hover:border-slate-700/60"
        >
          <div class="relative">
            <img
              src="https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=120&q=80"
              alt="Профиль"
              class="w-8 h-8 rounded-full object-cover ring-1 transition-all"
              :class="isAuthenticated ? 'ring-emerald-400/80' : 'ring-slate-600/60'"
            />
            <div
              class="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full ring-2 ring-[#060912]"
              :class="isAuthenticated ? 'bg-emerald-400' : 'bg-slate-500'"
            ></div>
          </div>
        </div>

        <!-- User Dropdown Menu -->
        <transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="opacity-0 scale-95 -translate-y-1"
          enter-to-class="opacity-100 scale-100 translate-y-0"
          leave-active-class="transition duration-100 ease-in"
          leave-from-class="opacity-100 scale-100 translate-y-0"
          leave-to-class="opacity-0 scale-95 -translate-y-1"
        >
          <div
            v-if="showDropdown"
            class="absolute right-0 mt-2 w-56 p-2 rounded-2xl bg-[#0e1631]/95 border border-indigo-500/40 shadow-2xl backdrop-blur-xl z-50 text-xs text-slate-200"
          >
            <!-- User Status Header -->
            <div class="px-3 py-2 border-b border-slate-700/40 mb-1">
              <div class="font-semibold text-white">
                {{ isAuthenticated ? (currentUser?.name || 'Сотрудник') : 'Гостевой режим' }}
              </div>
              <div class="text-[11px] text-slate-400 truncate mt-0.5">
                {{ isAuthenticated ? `+${currentUser?.phone}` : 'Вход не выполнен' }}
              </div>
            </div>

            <!-- Action buttons in dropdown -->
            <div class="space-y-0.5">
              <button
                v-if="!isAuthenticated"
                @click="handleOpenAuth"
                class="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-indigo-300 hover:bg-indigo-600/20 hover:text-white transition-colors cursor-pointer text-left font-medium"
              >
                <LogIn class="w-4 h-4" />
                <span>Войти по номеру СМС</span>
              </button>

              <button
                v-else
                @click="handleLogout"
                class="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-rose-300 hover:bg-rose-500/20 hover:text-white transition-colors cursor-pointer text-left font-medium"
              >
                <LogOut class="w-4 h-4" />
                <span>Выйти из аккаунта</span>
              </button>
            </div>
          </div>
        </transition>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Bell, Settings, LogIn, LogOut } from 'lucide-vue-next'
import MazoryLogo from './icons/MazoryLogo.vue'
import { useAuth } from '../composables/useAuth'

const emit = defineEmits<{
  (e: 'goHome'): void
  (e: 'toggleNotifications'): void
  (e: 'openAuth'): void
}>()

const { isAuthenticated, currentUser, logout } = useAuth()
const showDropdown = ref(false)

function handleOpenAuth() {
  showDropdown.value = false
  emit('openAuth')
}

function handleLogout() {
  showDropdown.value = false
  logout()
}
</script>
