<template>
  <div class="w-full max-w-3xl mx-auto px-4 select-none">
    <!-- Floating Frosted Glass Pill Input -->
    <form
      @submit.prevent="handleSubmit"
      class="relative flex items-center w-full h-14 px-4 rounded-full bg-[#0d152a]/75 backdrop-blur-xl border border-[#3b4c7d]/50 shadow-[0_8px_32px_rgba(0,0,0,0.5),0_0_20px_rgba(79,93,179,0.2)] focus-within:border-indigo-500/80 focus-within:shadow-[0_0_28px_rgba(99,102,241,0.35)] transition-all duration-300"
    >
      <!-- Sparkle AI Icon on left -->
      <div class="flex items-center justify-center w-8 h-8 text-indigo-400 shrink-0 mr-2">
        <Sparkles class="w-5 h-5 animate-pulse text-indigo-300 drop-shadow-[0_0_8px_rgba(129,140,248,0.7)]" />
      </div>

      <!-- Main Text Input -->
      <input
        ref="inputRef"
        v-model="queryText"
        type="text"
        :placeholder="placeholder || 'Спросите Mazory...'"
        class="flex-1 bg-transparent text-white placeholder-slate-400/80 text-[15px] focus:outline-none px-1 tracking-wide"
      />

      <!-- Right Action Tools -->
      <div class="flex items-center gap-1.5 shrink-0 ml-2">
        <!-- Paperclip (Attachment) -->
        <button
          type="button"
          class="p-2 text-slate-400 hover:text-indigo-200 transition-colors rounded-full hover:bg-white/5"
          title="Прикрепить файл"
          @click="$emit('attachFile')"
        >
          <Paperclip class="w-4 h-4" />
        </button>

        <!-- Microphone (Voice input) -->
        <button
          type="button"
          class="p-2 text-slate-400 hover:text-indigo-200 transition-colors rounded-full hover:bg-white/5"
          title="Голосовой ввод"
          @click="$emit('voiceInput')"
        >
          <Mic class="w-4 h-4" />
        </button>

        <!-- Submit Send Button -->
        <button
          type="submit"
          class="flex items-center justify-center w-9 h-9 rounded-full bg-[#1b254b]/90 text-indigo-200 border border-indigo-500/50 hover:bg-indigo-600 hover:text-white hover:border-indigo-400 shadow-[0_0_12px_rgba(99,102,241,0.35)] active:scale-95 transition-all ml-1 disabled:opacity-40 disabled:pointer-events-none cursor-pointer"
          :disabled="!queryText.trim()"
          title="Отправить запрос"
        >
          <ArrowUp class="w-4 h-4 stroke-[2.5]" />
        </button>
      </div>
    </form>

    <!-- Suggestion Chips Row -->
    <div
      v-if="suggestions && suggestions.length"
      class="flex flex-wrap items-center justify-center gap-2.5 mt-3 px-2"
    >
      <button
        v-for="(chip, index) in suggestions"
        :key="index"
        type="button"
        @click="handleChipClick(chip)"
        class="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full text-xs font-medium text-slate-300 bg-[#0d162d]/65 backdrop-blur-md border border-[#303f69]/50 hover:bg-[#182348]/85 hover:border-indigo-500/60 hover:text-white hover:shadow-[0_0_15px_rgba(99,102,241,0.25)] transition-all duration-200 cursor-pointer"
      >
        <span>{{ chip }}</span>
        <ArrowUpRight class="w-3 h-3 text-slate-400 group-hover:text-indigo-300" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Sparkles, Paperclip, Mic, ArrowUp, ArrowUpRight } from 'lucide-vue-next'

defineProps<{
  placeholder?: string
  suggestions?: string[]
}>()

const emit = defineEmits<{
  (e: 'submit', query: string): void
  (e: 'attachFile'): void
  (e: 'voiceInput'): void
}>()

const queryText = ref('')
const inputRef = ref<HTMLInputElement | null>(null)

function handleSubmit() {
  const trimmed = queryText.value.trim()
  if (!trimmed) return
  emit('submit', trimmed)
  queryText.value = ''
}

function handleChipClick(chip: string) {
  const clean = chip.replace(/[↗→↑]/g, '').trim()
  emit('submit', clean)
}
</script>
