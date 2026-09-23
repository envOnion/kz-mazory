<template>
  <div class="w-full max-w-7xl mx-auto px-4 md:px-6 py-4 flex flex-col gap-6 z-10">
    <!-- Header: AI Category badge, Query Title, Subtitle, Updated status -->
    <div class="flex flex-col md:flex-row md:items-end justify-between gap-4">
      <div>
        <!-- AI Badge -->
        <div class="inline-flex items-center gap-1.5 text-xs font-semibold tracking-wider uppercase text-indigo-300 mb-1.5">
          <Sparkles class="w-3.5 h-3.5 text-indigo-400 drop-shadow-[0_0_6px_rgba(129,140,248,0.8)]" />
          <span>{{ data.categoryBadge }}</span>
        </div>

        <!-- Main Query Title -->
        <h1 class="text-2xl md:text-3xl lg:text-4xl font-bold tracking-tight text-white font-sans">
          {{ data.queryTitle }}
        </h1>

        <!-- Subtitle -->
        <p class="text-sm md:text-base text-slate-400 mt-1 font-normal">
          {{ data.querySubtitle }}
        </p>
      </div>

      <!-- Updated timestamp badge -->
      <div class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-[#0d162d]/60 border border-[#26355b]/40 text-xs text-slate-300 self-start md:self-auto">
        <span class="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"></span>
        <span>{{ data.updatedAtText }}</span>
      </div>
    </div>

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

    <!-- 5 Managers Grid -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3.5">
      <ManagerCard
        v-for="manager in data.managers"
        :key="manager.id"
        :manager="manager"
      />
    </div>

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

          <!-- WhatsApp sent status feedback -->
          <div v-if="whatsappStatus" class="mb-3 p-2.5 rounded-xl bg-emerald-500/20 border border-emerald-400/40 text-xs text-emerald-300">
            {{ whatsappStatus }}
          </div>

          <div class="flex items-center justify-between pt-2 border-t border-slate-700/40">
            <button
              v-if="activeActionModal.canSendWhatsApp"
              @click="handleSendWhatsApp"
              :disabled="isSendingWhatsApp"
              class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-xs shadow-lg transition-colors cursor-pointer flex items-center gap-1.5"
            >
              <Send class="w-3.5 h-3.5" />
              <span>{{ isSendingWhatsApp ? 'Отправка...' : 'Отправить в WhatsApp (WAHA)' }}</span>
            </button>
            <div v-else></div>

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
import { Sparkles, BarChart2, Target, Users, TrendingUp, Send } from 'lucide-vue-next'
import type { KpiDashboardData } from '../types/chat'
import ManagerCard from './ManagerCard.vue'
import AiInsightCard from './AiInsightCard.vue'
import { useAuth } from '../composables/useAuth'

defineProps<{
  data: KpiDashboardData
}>()

const { sendWhatsAppAlert } = useAuth()
const isSendingWhatsApp = ref(false)
const whatsappStatus = ref('')

const activeActionModal = ref<{
  title: string
  content: string
  canSendWhatsApp?: boolean
  managerPhone?: string
  messageText?: string
} | null>(null)

function handleActionTrigger(actionId: string, _label: string) {
  whatsappStatus.value = ''
  if (actionId === 'why') {
    activeActionModal.value = {
      title: 'Анализ причин отставания (Алина Смирнова)',
      content:
        'Детальный разбор конверсий Алины Смирновой:\n\n' +
        '• Количество первичных звонков: 112 (на уровне средних показателей команды).\n' +
        '• Переход из презентации в выставление КП: 28% (среднее по отделу: 46%).\n' +
        '• Основное возражение клиентов: "Слишком сложно согласовать регламент внедрения".\n\n' +
        'Рекомендация ИИ: Подключить Максима Кузнецова к 2 ключевым сделкам с суммой свыше 1.5M ₽ для дожима.'
    }
  } else if (actionId === 'deals') {
    activeActionModal.value = {
      title: 'Реестр сделок команды',
      content:
        'Активные сделки на текущий момент:\n\n' +
        '1. ТОО "КазНефтеГазАвтоматика" — 3 200 000 ₽ (Максим Кузнецов) — Стадия: Договор\n' +
        '2. АО "ПромВодСтрой" — 2 100 000 ₽ (Ирина Волкова) — Стадия: Согласование КП\n' +
        '3. ТОО "Астана Энерго" — 1 800 000 ₽ (Даниил Соколов) — Стадия: Презентация\n' +
        '4. ТОО "Батыс КИП" — 900 000 ₽ (Алина Смирнова) — Стадия: Ожидание оплаты'
    }
  } else if (actionId === 'compare') {
    activeActionModal.value = {
      title: 'Сравнение с прошлым месяцем',
      content:
        'Показатели команды в динамике MoM:\n\n' +
        '• Выручка: 24.5M ₽ против 21.8M ₽ (+12.4% прирост)\n' +
        '• Средний чек сделки: 172 500 ₽ (рост на 4.2%)\n' +
        '• Цикл сделки сократился с 21 до 16 рабочих дней.'
    }
  } else {
    activeActionModal.value = {
      title: 'Коммуникация с сотрудником (WhatsApp)',
      content:
        'Черновик сообщения для Алины Смирновой:\n\n' +
        '"Привет, Алина! Заметил, что по текущим сделкам затянулся этап КП. Давай устроим 15-минутный синк с Максимом, он поделится свежими аргументами по новому каталогу."',
      canSendWhatsApp: true,
      managerPhone: '77011234567',
      messageText: 'Привет, Алина! Заметил, что по текущим сделкам затянулся этап КП. Давай устроим 15-минутный синк с Максимом, он поделится свежими аргументами.'
    }
  }
}

async function handleSendWhatsApp() {
  if (!activeActionModal.value?.managerPhone || !activeActionModal.value?.messageText) return
  isSendingWhatsApp.value = true
  whatsappStatus.value = ''
  try {
    const res = await sendWhatsAppAlert(
      activeActionModal.value.managerPhone,
      activeActionModal.value.messageText
    )
    if (res.status === 'queued') {
      whatsappStatus.value = '✓ Сообщение успешно отправлено в очередь Django Q & WAHA!'
    } else {
      whatsappStatus.value = `Статус: ${res.message || 'Отправлено'}`
    }
  } finally {
    isSendingWhatsApp.value = false
  }
}
</script>
