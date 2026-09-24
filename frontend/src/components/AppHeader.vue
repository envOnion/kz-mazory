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

    <!-- Right controls: notifications, auth/profile door & avatar (NO DROPDOWNS, NO GEAR) -->
    <div class="flex items-center gap-3">
      <!-- Notification Bell with real Redis count & Popover - ONLY for Authenticated users -->
      <div v-if="isAuthenticated" class="relative">
        <button
          type="button"
          class="relative p-2.5 text-slate-300 hover:text-white rounded-full hover:bg-white/5 transition-colors focus:outline-none cursor-pointer"
          title="Уведомления"
          @click="togglePopover"
        >
          <Bell class="w-5 h-5" />
          <!-- Real unread dot / badge from Redis -->
          <span
            v-if="unreadCount > 0"
            class="absolute top-1.5 right-1.5 min-w-[16px] h-4 px-1 rounded-full bg-cyan-400 text-[#060912] font-extrabold text-[10px] flex items-center justify-center shadow-[0_0_8px_rgba(34,211,238,0.9)]"
          >
            {{ unreadCount }}
          </span>
        </button>

        <!-- Working Notifications Popover -->
        <NotificationsPopover
          :is-open="isPopoverOpen"
          :notifications="notifications"
          :unread-count="unreadCount"
          @close="closePopover"
          @mark-all-read="markAllAsRead"
        />
      </div>

      <!-- Authentication / Profile Trigger (Direct action, NO DROPDOWNS): -->
      <!-- Case 1: NOT Authenticated -> Door Icon Button immediately opens Login modal -->
      <button
        v-if="!isAuthenticated"
        type="button"
        @click="emit('openAuth')"
        class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-indigo-600/20 border border-indigo-400/40 text-indigo-300 hover:bg-indigo-600 hover:text-white hover:border-indigo-400 shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all cursor-pointer text-xs font-medium"
        title="Войти по номеру телефона"
      >
        <DoorOpen class="w-4 h-4" />
        <span>Войти</span>
      </button>

      <!-- Case 2: Authenticated -> Avatar button immediately opens Personal Account (Личный кабинет) -->
      <button
        v-else
        type="button"
        @click="emit('openProfile')"
        class="relative group p-0.5 rounded-full hover:ring-2 hover:ring-indigo-400 transition-all cursor-pointer focus:outline-none"
        title="Личный кабинет"
      >
        <img
          :src="profile.avatar_url || 'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=120&q=80'"
          alt="Профиль"
          class="w-8 h-8 rounded-full object-cover ring-1 ring-emerald-400/80 shadow-md"
        />
        <div class="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-emerald-400 ring-2 ring-[#060912] shadow-[0_0_6px_rgba(52,211,153,0.8)]"></div>
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { Bell, DoorOpen } from 'lucide-vue-next'
import MazoryLogo from './icons/MazoryLogo.vue'
import NotificationsPopover from './NotificationsPopover.vue'
import { useAuth } from '../composables/useAuth'
import { useProfile } from '../composables/useProfile'
import { useNotifications } from '../composables/useNotifications'

const emit = defineEmits<{
  (e: 'goHome'): void
  (e: 'openAuth'): void
  (e: 'openProfile'): void
}>()

const { isAuthenticated } = useAuth()
const { profile } = useProfile()
const {
  notifications,
  unreadCount,
  isPopoverOpen,
  fetchNotifications,
  markAllAsRead,
  togglePopover,
  closePopover
} = useNotifications()

onMounted(() => {
  if (isAuthenticated.value) {
    fetchNotifications()
  }
})

watch(isAuthenticated, (authed) => {
  if (authed) {
    fetchNotifications()
  } else {
    closePopover()
  }
})

</script>
