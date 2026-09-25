<template>
  <div class="w-full max-w-7xl mx-auto px-4 md:px-6 py-4 flex flex-col gap-6 z-10">
    <!-- Header: AI Category badge, Query Title, Subtitle, Updated status -->
    <div class="flex flex-col md:flex-row md:items-end justify-between gap-4">
      <div>
        <!-- AI Badge -->
        <div class="inline-flex items-center gap-1.5 text-xs font-semibold tracking-wider uppercase text-indigo-300 mb-1.5">
          <Sparkles class="w-3.5 h-3.5 text-indigo-400 drop-shadow-[0_0_6px_rgba(129,140,248,0.8)]" />
          <span>{{ data.categoryBadge || 'AQUA KIP DATA MART' }}</span>
        </div>

        <!-- Main Query Title -->
        <h1 class="text-2xl md:text-3xl lg:text-4xl font-bold tracking-tight text-white font-sans">
          {{ data.queryTitle }}
        </h1>

        <!-- Subtitle or Chat AI response text -->
        <p class="text-sm md:text-base text-slate-300 mt-1 font-normal leading-relaxed">
          {{ responseText || data.querySubtitle }}
        </p>
      </div>

      <!-- Updated timestamp badge -->
      <div class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-[#0d162d]/60 border border-[#26355b]/40 text-xs text-slate-300 self-start md:self-auto">
        <span class="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"></span>
        <span>{{ data.updatedAtText }}</span>
      </div>
    </div>

    <!-- DYNAMIC PRESET WIDGET CONTAINER -->
    <transition
      mode="out-in"
      enter-active-class="transition duration-300 ease-out"
      enter-from-class="opacity-0 translate-y-3"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 -translate-y-3"
    >
      <!-- Preset 1: Interactive Chart.js (Bar / Line / Doughnut) -->
      <div v-if="widget && widget.type === 'chart'" class="w-full">
        <PresetChart :data="widget.data" />
      </div>

      <!-- Preset 2: Commitments & Deadlines SLA List -->
      <div v-else-if="widget && widget.type === 'commitments_list'" class="w-full">
        <PresetCommitmentList :data="widget.data" />
      </div>

      <!-- Preset 3: Deal Pipeline & Project Table -->
      <div v-else-if="widget && widget.type === 'project_table'" class="w-full">
        <PresetProjectTable :data="widget.data" />
      </div>

      <!-- Preset 4 (Default): 3 Summary Metrics + Manager KPI Grid -->
      <div v-else class="space-y-6">
        <!-- 3 Summary Metrics Cards -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div
            v-for="metric in data.summaryMetrics"
            :key="metric.id"
            class="flex items-center gap-4 p-5 rounded-2xl bg-[#0b1226]/80 backdrop-blur-xl border border-[#2d3a63]/40 shadow-lg hover:border-indigo-500/40 transition-all duration-300"
          >
            <!-- Icon badge -->
            <div class="flex items-center justify-center w-12 h-12 rounded-xl bg-[#141d3d] border border-[#2a3862]/60 text-indigo-300 shrink-0">
              <BarChart2 v-if="metric.icon === 'bar-chart'" class="w-6 h-6 text-indigo-300" />
              <Target v-else-if="metric.icon === 'target'" class="w-6 h-6 text-indigo-300" />
              <Users v-else-if="metric.icon === 'users'" class="w-6 h-6 text-indigo-300" />
            </div>

            <!-- Metric Details -->
            <div class="flex flex-col">
              <span class="text-xs text-slate-400 font-medium">{{ metric.title }}</span>
              <span class="text-2xl font-bold text-white tracking-tight mt-0.5">{{ metric.value }}</span>
              <div class="flex items-center gap-1 text-xs text-emerald-400 font-medium mt-1">
                <TrendingUp class="w-3.5 h-3.5" />
                <span>{{ metric.trend }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 5-6 Managers Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3.5">
          <ManagerCard
            v-for="manager in data.managers"
            :key="manager.id"
            :manager="manager"
          />
        </div>
      </div>
    </transition>

    <!-- AI Insight Card -->
    <AiInsightCard
      :insight="data.insight"
      @trigger-action="handleActionTrigger"
    />

    <!-- Interactive Action Result Modal / Drawer -->
    <transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 scale-95"
      enter-to-class="opacity-100 scale-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 scale-100"
      leave-to-class="opacity-0 scale-95"
    >
      <div
        v-if="activeActionModal"
        class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-md select-none"
        @click.self="activeActionModal = null"
      >
        <div class="w-full max-w-lg p-6 rounded-3xl bg-[#0f1733] border border-indigo-500/50 shadow-2xl text-slate-100">
          <div class="flex items-center justify-between pb-3 border-b border-slate-700/50">
            <h3 class="text-lg font-bold text-white flex items-center gap-2">
              <Sparkles class="w-5 h-5 text-indigo-400" />
              {{ activeActionModal.title }}
            </h3>
            <button
              @click="activeActionModal = null"
              class="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800"
            >
              ✕
            </button>
          </div>

          <div class="py-4 text-sm text-slate-300 leading-relaxed whitespace-pre-line">
            {{ activeActionModal.content }}
          </div>

          <div class="flex items-center justify-end pt-2 border-t border-slate-700/40">
            <button
              @click="activeActionModal = null"
              class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs shadow-lg transition-colors cursor-pointer"
            >
              Понятно
            </button>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Sparkles, BarChart2, Target, Users, TrendingUp } from 'lucide-vue-next'
import type { KpiDashboardData, ChatWidget } from '../types/chat'
import { useAuth } from '../composables/useAuth'
import ManagerCard from './ManagerCard.vue'
import AiInsightCard from './AiInsightCard.vue'
import PresetChart from './presets/PresetChart.vue'
import PresetCommitmentList from './presets/PresetCommitmentList.vue'
import PresetProjectTable from './presets/PresetProjectTable.vue'

defineProps<{
  data: KpiDashboardData
  widget?: ChatWidget | null
  responseText?: string
}>()

const emit = defineEmits<{
  (e: 'selectPrompt', prompt: string): void
}>()

const { isAuthenticated } = useAuth()

const activeActionModal = ref<{
  title: string
  content: string
} | null>(null)

function handleActionTrigger(actionId: string, _label: string) {
  if (actionId === 'chart') {
    emit('selectPrompt', 'Выведи график продаж по менеджерам')
  } else if (actionId === 'commitments') {
    emit('selectPrompt', 'Какие обещания и дедлайны горят?')
  } else if (actionId === 'deals') {
    emit('selectPrompt', 'Покажи воронку проектов')
  } else if (actionId === 'why') {
    if (!isAuthenticated.value) {
      emit('selectPrompt', 'Почему лидер по сбору денег — Жанат Бейсбаев?')
      return
    }
    activeActionModal.value = {
      title: 'Анализ факторов сбора оплат (Aqua Kip)',
      content:
        'Детальный разбор ключевых драйверов продаж:\n\n' +
        '• ПСЭМ 190 и ПСЭМ 100 дают свыше 2.32 млрд ₸ контрактной массы, но маржа 14.0–14.4% требует жесткого аудита себестоимости.\n' +
        '• Сбор оплат по Top Build (117 млн ₸) успешно закрыт благодаря поставке оборудования Danfoss в Семей.\n' +
        '• Потенциал новых контрактов: тендеры Алматы Су на 300 млн ₸ и котельная 10 МВт Vertex Garden на 156 млн ₸.'
    }
  }
}
</script>
