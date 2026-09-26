<template>
  <div class="flex-1 flex flex-col items-center justify-center -mt-16 px-4 z-10">
    <!-- Hero Branding -->
    <div class="flex flex-col items-center mb-8 select-none">
      <!-- Big Glowing Monogram -->
      <div class="relative mb-5 flex items-center justify-center">
        <div class="absolute inset-0 bg-indigo-500/20 blur-3xl rounded-full scale-150"></div>
        <MazoryLogo :size="76" :glow="true" />
      </div>

      <!-- Title -->
      <h1 class="text-4xl font-bold tracking-tight text-white mb-2 font-sans">
        Mazory
      </h1>

      <!-- Subtitle -->
      <p class="text-sm tracking-wide text-slate-400 font-medium">
        Your AI Business OS
      </p>
    </div>

    <!-- Centered Prompt Input Bar -->
    <ChatInput
      :suggestions="suggestions"
      :disabled="disabled"
      @submit="(query) => $emit('selectPrompt', query)"
      @attach-file="$emit('attachFile')"
      @voice-input="$emit('voiceInput')"
    />
  </div>
</template>

<script setup lang="ts">
import MazoryLogo from './icons/MazoryLogo.vue'
import ChatInput from './ChatInput.vue'

defineProps<{
  suggestions: string[]
  disabled?: boolean
}>()

defineEmits<{
  (e: 'selectPrompt', query: string): void
  (e: 'attachFile'): void
  (e: 'voiceInput'): void
}>()
</script>
