<template>
  <div class="relative w-full p-5 rounded-2xl bg-[#0c1328]/85 backdrop-blur-xl border border-[#2d3a63]/50 shadow-[0_8px_32px_rgba(0,0,0,0.4)] select-none">
    <!-- Header: Sparkle + AI-инсайт + Right source note -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
      <div class="flex items-center gap-2">
        <div class="flex items-center justify-center w-6 h-6 rounded-lg bg-indigo-500/15 text-indigo-300">
          <Sparkles class="w-4 h-4 drop-shadow-[0_0_8px_rgba(129,140,248,0.8)]" />
        </div>
        <span class="text-sm font-semibold text-white tracking-tight">
          {{ insight.badge }}
        </span>
      </div>

      <div class="text-xs text-slate-400 font-normal">
        {{ insight.source }}
      </div>
    </div>

    <!-- Text Insights -->
    <div class="space-y-1 mb-5">
      <p class="text-[15px] font-semibold text-white leading-relaxed">
        {{ insight.headline }}
      </p>
      <p class="text-[13px] text-slate-300 leading-normal">
        {{ insight.details }}
      </p>
    </div>

    <!-- Action Buttons -->
    <div class="flex flex-wrap items-center gap-2.5">
      <button
        v-for="action in insight.actions"
        :key="action.id"
        type="button"
        @click="$emit('triggerAction', action.id, action.label)"
        class="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-medium text-slate-200 bg-[#121c3b]/70 border border-[#32426e]/50 hover:bg-[#1a2854] hover:border-indigo-400/60 hover:text-white hover:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all duration-200 active:scale-95"
      >
        <Search v-if="action.icon === 'search'" class="w-3.5 h-3.5 text-indigo-300" />
        <FileText v-else-if="action.icon === 'file-text'" class="w-3.5 h-3.5 text-indigo-300" />
        <BarChart2 v-else-if="action.icon === 'bar-chart-2'" class="w-3.5 h-3.5 text-indigo-300" />
        <Send v-else-if="action.icon === 'send'" class="w-3.5 h-3.5 text-indigo-300" />
        <span>{{ action.label }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Sparkles, Search, FileText, BarChart2, Send } from 'lucide-vue-next'
import type { AiInsight } from '../types/chat'

defineProps<{
  insight: AiInsight
}>()

defineEmits<{
  (e: 'triggerAction', actionId: string, label: string): void
}>()
</script>
