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
        <div v-if="isLoading" class="mt-2 space-y-2">
          <div class="h-4 w-3/4 bg-slate-700/50 rounded animate-pulse"></div>
          <div class="h-4 w-1/2 bg-slate-700/50 rounded animate-pulse"></div>
        </div>
        <div
          v-else
          class="text-sm md:text-base text-slate-300 mt-1 font-normal leading-relaxed prose prose-invert prose-sm max-w-none [&>ul]:list-disc [&>ul]:pl-5 [&>ol]:list-decimal [&>ol]:pl-5 [&>p]:my-1"
          v-html="renderedResponse"
        ></div>
      </div>

      <!-- Actions: Period selector & Updated timestamp badge -->
      <div class="flex flex-wrap items-center gap-2.5 self-start md:self-auto">
        <!-- Period Switcher Pills -->
        <div class="flex items-center gap-1 p-1 rounded-xl bg-[#090f22]/80 border border-[#233154]/50 text-xs">
          <button
            v-for="p in periodOptions"
            :key="p.id"
            type="button"
            @click="$emit('changePeriod', p.id)"
            class="px-2.5 py-1 rounded-lg font-medium transition-all duration-200 cursor-pointer"
            :class="[
              activePeriod === p.id
                ? 'bg-indigo-600 text-white shadow-md shadow-indigo-500/25'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
            ]"
          >
            {{ p.label }}
          </button>
        </div>

        <!-- Updated timestamp badge -->
        <div class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-[#0d162d]/60 border border-[#26355b]/40 text-xs text-slate-300">
          <span class="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"></span>
          <span>{{ data.updatedAtText }}</span>
        </div>
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

        <!-- Embedded Interactive Chart.js Plan vs Fact by Managers -->
        <div v-if="data.chartData" class="w-full">
          <PresetChart :data="data.chartData" />
        </div>

        <!-- 5-6 Managers Grid with Drill-Down -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3.5">
          <ManagerCard
            v-for="manager in data.managers"
            :key="manager.id"
            :manager="manager"
            @select="openManagerDrillDown"
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

    <!-- Manager Drill-Down Modal / Drawer -->
    <transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 scale-95"
      enter-to-class="opacity-100 scale-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 scale-100"
      leave-to-class="opacity-0 scale-95"
    >
      <div
        v-if="selectedManager"
        class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-md select-none"
        @click.self="selectedManager = null"
      >
        <div class="w-full max-w-2xl max-h-[85vh] flex flex-col rounded-3xl bg-[#0c142c] border border-indigo-500/50 shadow-2xl text-slate-100 overflow-hidden">
          <!-- Modal Header -->
          <div class="flex items-center justify-between p-5 border-b border-slate-700/50 bg-[#090f22]/80">
            <div class="flex items-center gap-3.5">
              <img
                :src="selectedManager.avatar"
                :alt="selectedManager.name"
                class="w-12 h-12 rounded-2xl object-cover ring-2 ring-indigo-400/60 shadow"
              />
              <div>
                <div class="flex items-center gap-2">
                  <h3 class="text-base font-bold text-white tracking-tight">{{ selectedManager.name }}</h3>
                  <span v-if="selectedManager.isTopPerformer" class="px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 text-[11px] font-semibold border border-indigo-400/30">Лидер</span>
                </div>
                <p class="text-xs text-slate-400 mt-0.5">{{ selectedManager.role }}</p>
              </div>
            </div>
            <button
              @click="selectedManager = null"
              class="text-slate-400 hover:text-white p-1.5 rounded-xl hover:bg-slate-800 transition-colors"
            >
              ✕
            </button>
          </div>

          <!-- Body: Manager Stats & Deals -->
          <div class="p-5 overflow-y-auto space-y-5">
            <!-- 4 Mini Metric Cards -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
              <div class="p-3 rounded-xl bg-[#131d3d]/60 border border-slate-800/80">
                <div class="text-[10px] uppercase text-slate-400 font-semibold tracking-wider">Факт сбора</div>
                <div class="text-sm font-bold text-white mt-1">{{ selectedManager.salesAmount }}</div>
              </div>
              <div class="p-3 rounded-xl bg-[#131d3d]/60 border border-slate-800/80">
                <div class="text-[10px] uppercase text-slate-400 font-semibold tracking-wider">План сбора</div>
                <div class="text-sm font-bold text-white mt-1">{{ selectedManager.targetFormatted || (selectedManager.targetAmount ? `${(selectedManager.targetAmount / 1e6).toFixed(1)} млн ₸` : '10 млн ₸') }}</div>
              </div>
              <div class="p-3 rounded-xl bg-[#131d3d]/60 border border-slate-800/80">
                <div class="text-[10px] uppercase text-slate-400 font-semibold tracking-wider">KPI план</div>
                <div class="text-sm font-bold mt-1" :class="selectedManager.kpiPercent >= 100 ? 'text-emerald-400' : (selectedManager.kpiPercent >= 75 ? 'text-amber-400' : 'text-rose-400')">
                  {{ selectedManager.kpiPercent }}%
                </div>
              </div>
              <div class="p-3 rounded-xl bg-[#131d3d]/60 border border-slate-800/80">
                <div class="text-[10px] uppercase text-slate-400 font-semibold tracking-wider">Ср. маржа</div>
                <div class="text-sm font-bold text-indigo-300 mt-1">{{ selectedManager.averageMargin || 16.8 }}%</div>
              </div>
            </div>

            <!-- Active Projects List -->
            <div>
              <div class="flex items-center justify-between mb-2.5">
                <h4 class="text-xs uppercase tracking-wider font-semibold text-slate-400">
                  Закрепленные объекты и сделки ({{ selectedManager.projects ? selectedManager.projects.length : selectedManager.dealsCount }})
                </h4>
                <span v-if="selectedManager.overdueCommitments" class="text-xs text-rose-400 font-medium">
                  {{ selectedManager.overdueCommitments }} просроченных дедлайнов
                </span>
              </div>

              <div v-if="selectedManager.projects && selectedManager.projects.length > 0" class="divide-y divide-slate-800/60 rounded-2xl bg-[#090f22]/70 border border-slate-800 overflow-hidden">
                <div
                  v-for="proj in selectedManager.projects"
                  :key="proj.id"
                  class="p-3 hover:bg-slate-800/30 transition-colors flex items-center justify-between gap-3 text-xs"
                >
                  <div class="min-w-0 flex-1">
                    <div class="font-semibold text-white truncate">{{ proj.name }}</div>
                    <div class="text-[11px] text-slate-400 mt-0.5 truncate">{{ proj.company }} • {{ proj.status }}</div>
                  </div>
                  <div class="text-right shrink-0">
                    <div class="font-bold text-white">{{ proj.contract_formatted }}</div>
                    <div v-if="proj.due_amount > 0" class="text-[10px] text-amber-400 font-medium">
                      Долг: {{ proj.due_formatted }}
                    </div>
                    <div v-else class="text-[10px] text-emerald-400 font-medium">
                      Оплачено полностью
                    </div>
                  </div>
                </div>
              </div>
              <div v-else class="p-4 rounded-xl bg-slate-900/40 border border-slate-800 text-center text-xs text-slate-400">
                Нет детальных записей по объектам
              </div>
            </div>
          </div>

          <!-- Modal Footer with AI Quick Actions -->
          <div class="flex flex-wrap items-center justify-between gap-2 p-4 border-t border-slate-800 bg-[#090f22]/80">
            <div class="flex items-center gap-2">
              <button
                type="button"
                @click="askAiAboutManager(selectedManager)"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs shadow-md transition-colors cursor-pointer"
              >
                <Sparkles class="w-3.5 h-3.5" />
                <span>Спросить AI об этом менеджере</span>
              </button>
              <button
                type="button"
                @click="showManagerDeadlines(selectedManager)"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium text-xs transition-colors cursor-pointer"
              >
                <span>Дедлайны и задачи</span>
              </button>
            </div>
            <button
              type="button"
              @click="selectedManager = null"
              class="px-3.5 py-1.5 rounded-xl text-slate-400 hover:text-white text-xs font-medium cursor-pointer"
            >
              Закрыть
            </button>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { Sparkles, BarChart2, Target, Users, TrendingUp } from 'lucide-vue-next'
import type { KpiDashboardData, ChatWidget, ManagerKpi } from '../types/chat'
import { useAuth } from '../composables/useAuth'
import ManagerCard from './ManagerCard.vue'
import AiInsightCard from './AiInsightCard.vue'
import PresetChart from './presets/PresetChart.vue'
import PresetCommitmentList from './presets/PresetCommitmentList.vue'
import PresetProjectTable from './presets/PresetProjectTable.vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const props = defineProps<{
  data: KpiDashboardData
  widget?: ChatWidget | null
  responseText?: string
  isLoading?: boolean
  period?: string
}>()

const emit = defineEmits<{
  (e: 'selectPrompt', prompt: string): void
  (e: 'changePeriod', period: string): void
}>()

const { isAuthenticated } = useAuth()

const periodOptions = [
  { id: 'this_month', label: 'Этот месяц' },
  { id: 'last_month', label: 'Прошлый месяц' },
  { id: 'quarter', label: 'Квартал' },
  { id: 'year', label: 'С начала года' }
]

const activePeriod = computed(() => props.period || props.data.periodCode || 'this_month')

const selectedManager = ref<ManagerKpi | null>(null)

function openManagerDrillDown(manager: ManagerKpi) {
  selectedManager.value = manager
}

function askAiAboutManager(manager: ManagerKpi) {
  selectedManager.value = null
  emit('selectPrompt', `Покажи детальный аналитический отчет и сделки по менеджеру ${manager.name}`)
}

function showManagerDeadlines(manager: ManagerKpi) {
  selectedManager.value = null
  emit('selectPrompt', `Обязательства и дедлайны менеджера ${manager.name}`)
}

const renderedResponse = computed(() => {
  const text = props.responseText || props.data.querySubtitle
  if (!text) return ''
  const html = marked.parse(text, { breaks: true }) as string
  return DOMPurify.sanitize(html)
})

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
