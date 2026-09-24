<template>
  <div class="w-full bg-[#0b1021]/80 border border-slate-800/80 rounded-2xl p-5 shadow-2xl backdrop-blur-xl space-y-4">
    <!-- Header with Funnel summary -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-slate-800/60">
      <div>
        <h3 class="text-sm font-semibold text-white flex items-center gap-2">
          <Layers class="w-4 h-4 text-indigo-400" />
          <span>Воронка проектов и контроль экономики сделок</span>
        </h3>
        <p class="text-xs text-slate-400 mt-0.5">Портфель договоров Aqua Kip Engineering (суммы в тенге ₸)</p>
      </div>

      <!-- Margin alert summary badge -->
      <div v-if="data.margin_distribution?.low_under_15 > 0" class="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs">
        <AlertTriangle class="w-3.5 h-3.5 text-rose-400" />
        <span>{{ data.margin_distribution.low_under_15 }} сделок с маржой &lt; 15%</span>
      </div>
    </div>

    <!-- Funnel Stages Bar -->
    <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2">
      <div
        v-for="st in data.stages"
        :key="st.code"
        class="p-2.5 rounded-xl bg-slate-900/50 border border-slate-800/60 flex flex-col justify-between"
      >
        <span class="text-[11px] text-slate-400 truncate">{{ st.label }}</span>
        <div class="mt-1 flex items-baseline justify-between">
          <span class="text-sm font-bold text-white">{{ st.count }}</span>
          <span class="text-[10px] text-indigo-400 truncate max-w-[70px]">{{ st.volume_formatted }}</span>
        </div>
      </div>
    </div>

    <!-- Table of Projects -->
    <div class="overflow-x-auto max-h-96 pr-1">
      <table class="w-full text-left border-collapse text-xs">
        <thead>
          <tr class="border-b border-slate-800 text-slate-400">
            <th class="py-2.5 px-3 font-medium">Объект / Компания</th>
            <th class="py-2.5 px-3 font-medium">Менеджер</th>
            <th class="py-2.5 px-3 font-medium text-right">Сумма договора</th>
            <th class="py-2.5 px-3 font-medium text-right">Оплачено</th>
            <th class="py-2.5 px-3 font-medium text-right">К сбору</th>
            <th class="py-2.5 px-3 font-medium text-center">Маржа</th>
            <th class="py-2.5 px-3 font-medium">Статус</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/40">
          <tr
            v-for="p in data.projects"
            :key="p.id"
            class="hover:bg-slate-800/30 transition-colors"
          >
            <td class="py-3 px-3">
              <div class="font-medium text-slate-200">{{ p.name }}</div>
              <div class="text-[11px] text-slate-400 flex items-center gap-1.5 mt-0.5">
                <span>{{ p.company }}</span>
                <span class="text-slate-600">·</span>
                <span class="text-indigo-400/90">{{ p.equipment }}</span>
              </div>
            </td>
            <td class="py-3 px-3 text-slate-300 font-medium">
              {{ p.manager }}
            </td>
            <td class="py-3 px-3 text-right text-slate-200 font-semibold font-mono">
              {{ p.contract_formatted }}
            </td>
            <td class="py-3 px-3 text-right text-emerald-400 font-mono font-medium">
              {{ p.paid_formatted }}
            </td>
            <td class="py-3 px-3 text-right text-amber-400/90 font-mono font-medium">
              {{ p.due_formatted }}
            </td>
            <td class="py-3 px-3 text-center">
              <span
                class="px-2 py-0.5 rounded-md font-bold text-[11px]"
                :class="p.margin_alert ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40' : 'bg-slate-800 text-slate-300'"
              >
                {{ p.margin_percent }}%
              </span>
            </td>
            <td class="py-3 px-3">
              <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] bg-slate-800 text-slate-300 border border-slate-700/60">
                {{ p.status }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Layers, AlertTriangle } from 'lucide-vue-next'

interface ProjectItem {
  id: number
  name: string
  company: string
  manager: string
  contract_amount: number
  contract_formatted: string
  paid_amount: number
  paid_formatted: string
  due_amount: number
  due_formatted: string
  margin_percent: number
  margin_alert: boolean
  status: string
  priority: string
  equipment: string
}

interface StageItem {
  code: string
  label: string
  count: number
  volume: number
  volume_formatted: string
}

interface Props {
  data: {
    stages: StageItem[]
    margin_distribution: {
      low_under_15: number
      norm_15_to_20: number
      high_over_20: number
    }
    projects: ProjectItem[]
  }
}

defineProps<Props>()
</script>
