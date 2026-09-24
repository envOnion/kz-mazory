<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h3 class="text-base font-semibold text-white">Личные показатели и KPI</h3>
        <p class="text-xs text-slate-400 mt-0.5">Текущий прогресс выполнения плана продаж за месяц</p>
      </div>
      <div class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-indigo-500/20 border border-indigo-400/40 text-xs text-indigo-200 font-medium">
        <Trophy class="w-3.5 h-3.5 text-indigo-300" />
        <span>Рейтинг: #{{ profile.rank_in_team }} в команде</span>
      </div>
    </div>

    <!-- Main Highlight Progress Banner -->
    <div class="p-5 rounded-2xl bg-gradient-to-r from-[#111938] to-[#151f46] border border-indigo-500/40 shadow-xl">
      <div class="flex flex-col sm:flex-row sm:items-end justify-between gap-2 mb-3">
        <div>
          <span class="text-xs text-slate-400 font-medium">Выполнение плана продаж</span>
          <div class="text-3xl font-extrabold text-white tracking-tight mt-0.5">
            {{ profile.kpi_percent }}%
          </div>
        </div>
        <div class="text-xs text-slate-300 sm:text-right">
          <span class="text-emerald-400 font-semibold">{{ profile.current_sales_formatted }}</span>
          <span class="text-slate-500"> / {{ profile.monthly_target_formatted }}</span>
        </div>
      </div>

      <!-- Progress bar -->
      <div class="w-full h-2.5 rounded-full bg-[#0a0f20] overflow-hidden p-0.5">
        <div
          class="h-full rounded-full bg-gradient-to-r from-indigo-500 via-sky-400 to-emerald-400 shadow-[0_0_12px_rgba(52,211,153,0.8)] transition-all duration-700"
          :style="{ width: `${Math.min(100, profile.kpi_percent)}%` }"
        ></div>
      </div>

      <div class="flex items-center justify-between mt-2.5 text-[11px] text-slate-400">
        <span>Старт месяца: 0 ₽</span>
        <span class="text-emerald-300">План перевыполнен на +4% 🔥</span>
        <span>Цель: {{ profile.monthly_target_formatted }}</span>
      </div>
    </div>

    <!-- 4 Key Stat Cards -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3.5">
      <div class="p-4 rounded-xl bg-[#0e1633]/70 border border-[#2d3a63]/40">
        <div class="text-[11px] text-slate-400">Сделок закрыто</div>
        <div class="text-xl font-bold text-white mt-1">{{ profile.deals_count }}</div>
        <div class="text-[10px] text-emerald-400 mt-0.5">↗ +4 к прошлой неделе</div>
      </div>

      <div class="p-4 rounded-xl bg-[#0e1633]/70 border border-[#2d3a63]/40">
        <div class="text-[11px] text-slate-400">Конверсия воронки</div>
        <div class="text-xl font-bold text-white mt-1">{{ profile.conversion_rate }}%</div>
        <div class="text-[10px] text-emerald-400 mt-0.5">↗ Выше средней по отделу</div>
      </div>

      <div class="p-4 rounded-xl bg-[#0e1633]/70 border border-[#2d3a63]/40">
        <div class="text-[11px] text-slate-400">Средний чек</div>
        <div class="text-xl font-bold text-white mt-1">278 500 ₽</div>
        <div class="text-[10px] text-slate-400 mt-0.5">По 28 договорам</div>
      </div>

      <div class="p-4 rounded-xl bg-[#0e1633]/70 border border-[#2d3a63]/40">
        <div class="text-[11px] text-slate-400">Лидерборд</div>
        <div class="text-xl font-bold text-amber-300 mt-1">1 место</div>
        <div class="text-[10px] text-amber-400/80 mt-0.5">Лидер месяца 👑</div>
      </div>
    </div>

    <!-- Historical Performance -->
    <div class="p-4 rounded-2xl bg-[#0e1633]/50 border border-[#2d3a63]/40 space-y-3">
      <div class="text-xs font-semibold text-white">Динамика за 3 месяца</div>
      <div class="space-y-2 text-xs">
        <div class="flex items-center justify-between text-slate-300">
          <span>Сентябрь (текущий)</span>
          <span class="font-medium text-emerald-400">104% (7 800 000 ₽)</span>
        </div>
        <div class="flex items-center justify-between text-slate-400">
          <span>Август</span>
          <span>98% (7 100 000 ₽)</span>
        </div>
        <div class="flex items-center justify-between text-slate-400">
          <span>Июль</span>
          <span>92% (6 800 000 ₽)</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Trophy } from 'lucide-vue-next'
import { useProfile } from '../../composables/useProfile'

const { profile } = useProfile()
</script>
