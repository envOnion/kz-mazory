<template>
  <div class="w-full bg-[#0b1021]/80 border border-slate-800/80 rounded-2xl p-5 shadow-2xl backdrop-blur-xl space-y-4">
    <!-- Header with SLA Counters -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-slate-800/60">
      <div>
        <h3 class="text-sm font-semibold text-white flex items-center gap-2">
          <Clock class="w-4 h-4 text-amber-400" />
          <span>Контроль обещаний и дедлайнов (SLA)</span>
        </h3>
        <p class="text-xs text-slate-400 mt-0.5">Фиксация договоренностей из WhatsApp-переписок менеджеров</p>
      </div>
      <div class="flex items-center gap-2">
        <div class="px-2.5 py-1 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs">
          Выполнено: <strong class="font-bold">{{ data.fulfilled_count || 0 }}</strong>
        </div>
        <div class="px-2.5 py-1 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs">
          Просрочено: <strong class="font-bold">{{ data.overdue_count || 0 }}</strong>
        </div>
      </div>
    </div>

    <!-- Items List -->
    <div class="space-y-2.5 max-h-96 overflow-y-auto pr-1">
      <div
        v-for="item in data.commitments"
        :key="item.id"
        class="p-3.5 rounded-xl border bg-slate-900/40 hover:bg-slate-800/40 transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-3"
        :class="{
          'border-rose-500/30 bg-rose-500/5': item.status_color === 'red',
          'border-emerald-500/30 bg-emerald-500/5': item.status_color === 'green',
          'border-amber-500/30 bg-amber-500/5': item.status_color === 'yellow'
        }"
      >
        <div class="space-y-1">
          <div class="flex items-center gap-2 flex-wrap">
            <span
              class="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full"
              :class="{
                'bg-rose-500/20 text-rose-300 border border-rose-500/30': item.status_color === 'red',
                'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30': item.status_color === 'green',
                'bg-amber-500/20 text-amber-300 border border-amber-500/30': item.status_color === 'yellow'
              }"
            >
              {{ item.status }}
            </span>
            <span class="text-xs font-semibold text-slate-300">{{ item.manager_name }}</span>
            <span v-if="item.project_name" class="text-xs text-indigo-400 font-medium">· {{ item.project_name }}</span>
          </div>
          <p class="text-xs text-slate-200 leading-relaxed font-normal">{{ item.text }}</p>
        </div>

        <div class="flex items-center gap-3 shrink-0 text-right">
          <div>
            <div class="text-[11px] text-slate-400">Дедлайн</div>
            <div
              class="text-xs font-medium"
              :class="item.status_color === 'red' ? 'text-rose-400 font-bold' : 'text-slate-300'"
            >
              {{ item.deadline_formatted }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Clock } from 'lucide-vue-next'

interface CommitmentItem {
  id: number
  text: string
  counterparty?: string
  project_name: string
  manager_name: string
  deadline?: string
  deadline_formatted: string
  status: string
  status_color: 'red' | 'green' | 'yellow'
  severity: string
}

interface Props {
  data: {
    total_count: number
    fulfilled_count: number
    pending_count: number
    overdue_count: number
    slippage_rate_percent: number
    commitments: CommitmentItem[]
  }
}

defineProps<Props>()
</script>
