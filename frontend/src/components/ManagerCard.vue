<template>
  <div
    class="relative flex flex-col p-4 rounded-2xl bg-[#0b1226]/80 backdrop-blur-xl border transition-all duration-300 select-none group"
    :class="[
      manager.isTopPerformer
        ? 'border-indigo-500/70 shadow-[0_0_25px_rgba(99,102,241,0.25),inset_0_0_15px_rgba(99,102,241,0.1)] ring-1 ring-indigo-500/40'
        : 'border-[#2d3a63]/40 hover:border-indigo-500/40 hover:bg-[#0f1733]/90 shadow-lg'
    ]"
  >
    <!-- Top Row: Best performer badge or empty spacer + Status dot -->
    <div class="flex items-center justify-between w-full h-6 mb-2">
      <div v-if="manager.isTopPerformer" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-indigo-500/20 border border-indigo-400/40 text-[11px] font-medium text-indigo-200">
        <Crown class="w-3 h-3 text-indigo-300" />
        <span>Лучший результат</span>
      </div>
      <div v-else class="w-1"></div>

      <!-- Online / Status Dot -->
      <span
        class="w-2 h-2 rounded-full shrink-0"
        :class="{
          'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]': manager.statusColor === 'green',
          'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)]': manager.statusColor === 'yellow',
          'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.8)]': manager.statusColor === 'red'
        }"
      ></span>
    </div>

    <!-- Manager Avatar & Info -->
    <div class="flex flex-col items-center text-center mb-4">
      <div class="relative mb-2.5">
        <img
          :src="manager.avatar"
          :alt="manager.name"
          class="w-16 h-16 rounded-2xl object-cover ring-2 ring-slate-700/60 shadow-md group-hover:ring-indigo-400/80 transition-all duration-300"
        />
      </div>
      <h3 class="text-[15px] font-bold text-white tracking-tight leading-snug">
        {{ manager.name }}
      </h3>
      <p class="text-[12px] text-slate-400 mt-0.5">
        {{ manager.role }}
      </p>
    </div>

    <!-- KPI Progress Section -->
    <div class="mb-4">
      <div class="text-[11px] text-slate-400 mb-0.5 font-medium">
        Выполнение KPI
      </div>
      <div class="text-xl font-bold text-white tracking-tight mb-2">
        {{ manager.kpiPercent }}%
      </div>

      <!-- Progress bar track -->
      <div class="w-full h-1.5 rounded-full bg-[#16213e] overflow-hidden">
        <div
          class="h-full rounded-full transition-all duration-700"
          :style="{ width: `${Math.min(100, manager.kpiPercent)}%` }"
          :class="{
            'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.7)]': manager.kpiBarColor === 'green',
            'bg-amber-400 shadow-[0_0_10px_rgba(251,191,36,0.7)]': manager.kpiBarColor === 'yellow',
            'bg-rose-500 shadow-[0_0_10px_rgba(244,63,94,0.7)]': manager.kpiBarColor === 'red'
          }"
        ></div>
      </div>
    </div>

    <!-- Dual Metric Columns: Продажи & Сделок -->
    <div class="grid grid-cols-2 gap-2 py-2 border-t border-[#263354]/50 mb-2">
      <div>
        <div class="text-[10px] text-slate-400 uppercase tracking-wider font-medium">Продажи</div>
        <div class="text-[13px] font-semibold text-white tracking-tight mt-0.5">
          {{ manager.salesAmount }}
        </div>
      </div>
      <div>
        <div class="text-[10px] text-slate-400 uppercase tracking-wider font-medium">Сделок</div>
        <div class="text-[13px] font-semibold text-white tracking-tight mt-0.5">
          {{ manager.dealsCount }}
        </div>
      </div>
    </div>

    <!-- Trend vs previous month -->
    <div class="mt-auto flex items-center gap-1.5 text-[11px] font-medium pt-1">
      <TrendingUp v-if="manager.trendPositive" class="w-3.5 h-3.5 text-emerald-400 shrink-0" />
      <TrendingDown v-else class="w-3.5 h-3.5 text-rose-400 shrink-0" />
      <span :class="manager.trendPositive ? 'text-emerald-400' : 'text-rose-400'">
        {{ manager.trend }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Crown, TrendingUp, TrendingDown } from 'lucide-vue-next'
import type { ManagerKpi } from '../types/chat'

defineProps<{
  manager: ManagerKpi
}>()
</script>
