<template>
  <transition
    enter-active-class="transition duration-150 ease-out"
    enter-from-class="opacity-0 scale-95 -translate-y-1"
    enter-to-class="opacity-100 scale-100 translate-y-0"
    leave-active-class="transition duration-100 ease-in"
    leave-from-class="opacity-100 scale-100 translate-y-0"
    leave-to-class="opacity-0 scale-95 -translate-y-1"
  >
    <div
      v-if="isOpen"
      class="absolute right-0 mt-3 w-80 sm:w-96 rounded-3xl bg-[#0c142c]/95 border border-indigo-500/40 shadow-[0_16px_50px_rgba(0,0,0,0.7),0_0_30px_rgba(99,102,241,0.25)] backdrop-blur-2xl z-50 text-slate-100 overflow-hidden select-none"
    >
      <!-- Popover Header -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-slate-700/50 bg-[#090f22]/70">
        <div class="flex items-center gap-2">
          <Bell class="w-4 h-4 text-indigo-400" />
          <span class="text-xs font-bold text-white tracking-tight">Персональные уведомления</span>
          <span
            v-if="unreadCount > 0"
            class="px-1.5 py-0.2 rounded-full bg-cyan-400 text-[10px] font-black text-[#060912] shadow-[0_0_8px_rgba(34,211,238,0.9)]"
          >
            {{ unreadCount }}
          </span>
        </div>

        <button
          v-if="unreadCount > 0"
          type="button"
          @click="$emit('markAllRead')"
          class="text-[11px] text-indigo-300 hover:text-white transition-colors cursor-pointer"
        >
          Прочитать все
        </button>
      </div>

      <!-- Notifications List -->
      <div class="max-h-[380px] overflow-y-auto divide-y divide-slate-800/60 p-1">
        <div
          v-for="item in notifications"
          :key="item.id"
          class="p-3 rounded-2xl hover:bg-white/5 transition-all flex items-start gap-3 cursor-default"
          :class="{ 'bg-indigo-950/20': !item.is_read }"
        >
          <!-- Type Icon -->
          <div
            class="w-7 h-7 rounded-xl flex items-center justify-center shrink-0 mt-0.5"
            :class="{
              'bg-rose-500/15 text-rose-400 border border-rose-500/30': item.type === 'urgent',
              'bg-cyan-500/15 text-cyan-400 border border-cyan-500/30': item.type === 'deal',
              'bg-amber-500/15 text-amber-400 border border-amber-500/30': item.type === 'warning',
              'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30': item.type === 'kpi' || item.type === 'success',
              'bg-indigo-500/15 text-indigo-400 border border-indigo-500/30': item.type === 'info'
            }"
          >
            <AlertOctagon v-if="item.type === 'urgent'" class="w-3.5 h-3.5" />
            <Briefcase v-else-if="item.type === 'deal'" class="w-3.5 h-3.5" />
            <AlertTriangle v-else-if="item.type === 'warning'" class="w-3.5 h-3.5" />
            <Trophy v-else-if="item.type === 'kpi' || item.type === 'success'" class="w-3.5 h-3.5" />
            <Sparkles v-else class="w-3.5 h-3.5" />
          </div>

          <!-- Content -->
          <div class="flex-1 min-w-0">
            <div class="flex items-center justify-between gap-1">
              <span class="text-xs font-semibold text-white truncate">{{ item.title }}</span>
              <span class="text-[10px] text-slate-500 shrink-0">{{ item.time }}</span>
            </div>
            <p class="text-[11px] text-slate-300 mt-0.5 leading-snug">
              {{ item.message }}
            </p>
          </div>

          <!-- Unread Dot -->
          <span
            v-if="!item.is_read"
            class="w-1.5 h-1.5 rounded-full bg-cyan-400 shadow-[0_0_6px_rgba(34,211,238,0.8)] shrink-0 self-center"
          ></span>
        </div>

        <!-- Empty State (No mocks) -->
        <div
          v-if="!notifications || notifications.length === 0"
          class="p-8 text-center flex flex-col items-center justify-center gap-2 text-slate-400 select-none"
        >
          <div class="w-10 h-10 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center text-slate-500">
            <Bell class="w-5 h-5 opacity-40 text-slate-400" />
          </div>
          <div class="text-xs font-semibold text-slate-200">Нет новых уведомлений</div>
          <p class="text-[11px] text-slate-500 max-w-[210px] leading-relaxed">
            Здесь появляются реальные бизнес-события и персональные алерты, адресованные вам
          </p>
        </div>
      </div>

      <!-- Popover Footer -->
      <div class="px-4 py-2 border-t border-slate-800/60 bg-[#090f22]/50 text-[10px] text-slate-500 flex items-center justify-between">
        <span>Адресная очередь Django Q & Redis</span>
        <button
          type="button"
          @click="$emit('close')"
          class="hover:text-slate-300 transition-colors cursor-pointer"
        >
          Закрыть
        </button>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { Bell, AlertTriangle, Trophy, Sparkles, AlertOctagon, Briefcase } from 'lucide-vue-next'
import type { NotificationItem } from '../composables/useNotifications'

defineProps<{
  isOpen: boolean
  notifications: NotificationItem[]
  unreadCount: number
}>()

defineEmits<{
  (e: 'close'): void
  (e: 'markAllRead'): void
}>()
</script>

