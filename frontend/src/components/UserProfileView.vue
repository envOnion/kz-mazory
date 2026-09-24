<template>
  <div class="w-full max-w-6xl mx-auto px-4 md:px-6 py-6 z-10 select-none">
    <!-- Top Bar with Back Button -->
    <div class="flex items-center justify-between mb-6">
      <button
        type="button"
        @click="$emit('backToChat')"
        class="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-[#0d162d]/70 border border-[#2d3a63]/50 text-xs font-medium text-slate-300 hover:text-white hover:border-indigo-400 hover:bg-[#152248] transition-all cursor-pointer shadow-md"
      >
        <ArrowLeft class="w-4 h-4" />
        <span>Назад в чат Mazory</span>
      </button>

      <div class="text-xs text-slate-400">
        Личный кабинет • {{ profile.full_name }}
      </div>
    </div>

    <!-- Main Profile Layout: items-start prevents left column jumping -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
      <!-- Left Sidebar Column (4 of 12) - pinned with self-start and sticky -->
      <div class="lg:col-span-4 space-y-4 self-start sticky top-6">
        <!-- Identity Summary Card -->
        <div class="p-6 rounded-3xl bg-[#0b1226]/85 backdrop-blur-xl border border-[#2d3a63]/50 shadow-2xl flex flex-col items-center text-center">
          <div class="relative mb-3.5">
            <img
              :src="profile.avatar_url"
              :alt="profile.full_name"
              class="w-20 h-20 rounded-3xl object-cover ring-2 ring-indigo-500/60 shadow-xl"
            />
            <div class="absolute -bottom-1 -right-1 w-4 h-4 rounded-full bg-emerald-400 ring-4 ring-[#0b1226] shadow-[0_0_8px_rgba(52,211,153,0.8)]"></div>
          </div>

          <h2 class="text-lg font-bold text-white tracking-tight">
            {{ profile.full_name }}
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">
            {{ profile.role }}
          </p>
          <span class="inline-block mt-2 px-2.5 py-0.5 rounded-full bg-indigo-500/15 border border-indigo-400/30 text-[11px] text-indigo-300">
            {{ profile.department }}
          </span>

          <!-- Quick KPI Pill in Sidebar -->
          <div class="w-full mt-5 pt-4 border-t border-slate-700/40 grid grid-cols-2 gap-2 text-left">
            <div>
              <div class="text-[10px] text-slate-400 uppercase">План продаж</div>
              <div class="text-sm font-bold text-emerald-400 mt-0.5">{{ profile.kpi_percent }}%</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-400 uppercase">Место в топе</div>
              <div class="text-sm font-bold text-amber-300 mt-0.5">#{{ profile.rank_in_team }} 👑</div>
            </div>
          </div>
        </div>

        <!-- Navigation Tabs Menu -->
        <div class="p-2 rounded-2xl bg-[#0b1226]/75 backdrop-blur-xl border border-[#2d3a63]/40 shadow-lg space-y-1">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            type="button"
            @click="activeTab = tab.id"
            class="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-xs font-medium transition-all cursor-pointer text-left"
            :class="activeTab === tab.id
              ? 'bg-indigo-600/30 text-white border border-indigo-400/50 shadow-[0_0_15px_rgba(99,102,241,0.25)]'
              : 'text-slate-400 hover:text-white hover:bg-white/5 border border-transparent'"
          >
            <component :is="tab.icon" class="w-4 h-4 shrink-0 text-indigo-300" />
            <span>{{ tab.label }}</span>
          </button>
        </div>

        <!-- Logout Button in Sidebar at bottom-left -->
        <button
          type="button"
          @click="handleLogout"
          class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-2xl bg-rose-500/10 border border-rose-500/30 text-rose-300 hover:bg-rose-500/20 hover:border-rose-400 hover:text-white transition-all text-xs font-medium cursor-pointer shadow-md"
        >
          <LogOut class="w-4 h-4" />
          <span>Выйти из аккаунта</span>
        </button>
      </div>

      <!-- Right Content Panel (8 of 12) -->
      <div class="lg:col-span-8 p-6 md:p-8 rounded-3xl bg-[#0b1226]/85 backdrop-blur-xl border border-[#2d3a63]/50 shadow-2xl min-h-[520px]">
        <transition
          mode="out-in"
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="opacity-0 translate-y-1"
          enter-to-class="opacity-100 translate-y-0"
          leave-active-class="transition duration-100 ease-in"
          leave-from-class="opacity-100 translate-y-0"
          leave-to-class="opacity-0 -translate-y-1"
        >
          <component
            :is="activeTabComponent"
            @logged-out="handleLogout"
          />
        </transition>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  ArrowLeft,
  User,
  TrendingUp,
  Bell,
  Sparkles,
  ShieldCheck,
  LogOut
} from 'lucide-vue-next'
import { useProfile } from '../composables/useProfile'
import { useAuth } from '../composables/useAuth'

import ProfileGeneralTab from './profile/ProfileGeneralTab.vue'
import ProfileKpiTab from './profile/ProfileKpiTab.vue'
import ProfileNotificationsTab from './profile/ProfileNotificationsTab.vue'
import ProfileAiSettingsTab from './profile/ProfileAiSettingsTab.vue'
import ProfileSecurityTab from './profile/ProfileSecurityTab.vue'

import { useNotifications } from '../composables/useNotifications'

onMounted(() => {
  fetchProfile()
})

const emit = defineEmits<{
  (e: 'backToChat'): void
  (e: 'loggedOut'): void
}>()

const { profile, fetchProfile } = useProfile()
const { logout } = useAuth()
const { clearNotifications } = useNotifications()

const activeTab = ref<'general' | 'kpi' | 'notifications' | 'ai' | 'security'>('general')

const tabs = [
  { id: 'general', label: 'Профиль и контакты', icon: User },
  { id: 'kpi', label: 'Мои KPI и статистика', icon: TrendingUp },
  { id: 'notifications', label: 'Уведомления и WhatsApp', icon: Bell },
  { id: 'ai', label: 'Настройки ИИ Mazory', icon: Sparkles },
  { id: 'security', label: 'Безопасность и сессии', icon: ShieldCheck }
] as const

const activeTabComponent = computed(() => {
  switch (activeTab.value) {
    case 'general':
      return ProfileGeneralTab
    case 'kpi':
      return ProfileKpiTab
    case 'notifications':
      return ProfileNotificationsTab
    case 'ai':
      return ProfileAiSettingsTab
    case 'security':
      return ProfileSecurityTab
    default:
      return ProfileGeneralTab
  }
})

function handleLogout() {
  logout()
  clearNotifications()
  emit('loggedOut')
}
</script>

