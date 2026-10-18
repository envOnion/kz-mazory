<script setup lang="ts">
import { onMounted, ref } from 'vue'
import KpiDashboardView from '../components/KpiDashboardView.vue'
import ChatInput from '../components/ChatInput.vue'
import ChatWorkspace from '../components/ChatWorkspace.vue'
import { api } from '../composables/api'
import { useChat } from '../composables/useChat'
import type { Source } from '../types/platform'

// Сохранённый аналитический AI-чат (KPI, диалоги, артефакты) — внутри
// нового раздела «Аналитика», с теми же правами и эндпоинтами.
const {
  isGenerating, dashboardSuggestions, selectedPeriod, kpiData,
  activeWidget, activePresentation, hasChatResponse, chatResponseText,
  fetchKpiData, handlePromptSubmit, error, quotes, cancel, changeKpiFilters,
  turns, conversations, conversationId, selectedTurnId, selectedArtifact,
  selectTurn, followup, loadConversation, loadOlderTurns, hasOlderTurns, loadingOlderTurns, newConversation, restoreConversations,
} = useChat()

const sourceDialog = ref<HTMLDialogElement>()
const sourceText = ref('')
const notice = ref('')
async function openQuote(id: number) {
  try {
    const source = await api<Source>(`/messages/${id}/`)
    sourceText.value = `${source.sender_name} · ${source.sent_at || 'Время неизвестно'}\n\n${source.content}`
    sourceDialog.value?.showModal()
  } catch (e) { notice.value = e instanceof Error ? e.message : 'Источник недоступен' }
}

onMounted(() => { fetchKpiData(); restoreConversations() })
</script>

<template>
  <div class="flex flex-col gap-3">
    <p v-if="notice" role="status" class="mx-chip mx-chip-warn self-start">{{ notice }}</p>

    <ChatWorkspace
      v-if="hasChatResponse"
      :turns="turns" :conversations="conversations" :conversation-id="conversationId"
      :selected-turn-id="selectedTurnId" :artifact="selectedArtifact" :presentation="activePresentation"
      :is-generating="isGenerating" :error="error" :period="selectedPeriod"
      :has-older-turns="hasOlderTurns" :loading-older-turns="loadingOlderTurns"
      @submit="handlePromptSubmit" @select-turn="selectTurn" @followup="followup"
      @load-conversation="loadConversation" @load-older-turns="loadOlderTurns" @new-conversation="newConversation" @cancel="cancel"
      @open-source="openQuote" @change-filters="changeKpiFilters" @change-period="fetchKpiData"
    />
    <template v-else>
      <KpiDashboardView
        :data="kpiData" :widget="activeWidget" :presentation="activePresentation"
        :has-chat-response="hasChatResponse" :response-text="chatResponseText"
        :is-loading="isGenerating" :period="selectedPeriod"
        @open-source="openQuote" @change-period="fetchKpiData" @change-filters="changeKpiFilters"
        @select-prompt="handlePromptSubmit"
      />
      <ChatInput
        :suggestions="dashboardSuggestions" placeholder="Спросите Mazory..."
        :disabled="isGenerating" @submit="handlePromptSubmit"
      />
    </template>

    <p v-if="error && !hasChatResponse" role="alert" class="mx-chip mx-chip-err self-start">✕ {{ error }}</p>
    <button v-if="isGenerating && !hasChatResponse" class="mx-btn self-start" @click="cancel()">Отменить запрос</button>

    <dialog ref="sourceDialog" class="mx-card p-4 max-w-2xl backdrop:bg-black/70">
      <button class="mx-btn mb-3" @click="sourceDialog?.close()">Закрыть источник</button>
      <pre class="whitespace-pre-wrap">{{ sourceText }}</pre>
    </dialog>

    <div v-if="quotes.length" class="flex flex-col gap-2">
      <div v-for="quote in quotes" :key="quote.id" class="mx-card p-3 text-[12px]">
        <p style="white-space:pre-wrap">{{ quote.content }}</p>
        <p class="mx-muted mt-1">{{ quote.sender_name }} · {{ quote.sent_at }} ·
          <button class="mx-link" @click="openQuote(quote.id)">Открыть источник #{{ quote.id }}</button>
        </p>
      </div>
    </div>
  </div>
</template>
