<template>
  <div class="space-y-6">
    <div>
      <h3 class="text-base font-semibold text-white">Персонализация ИИ Mazory</h3>
      <p class="text-xs text-slate-400 mt-0.5">Настройка формата ответов и аналитических выводов под ваш стиль работы</p>
    </div>

    <!-- AI Response Style Cards -->
    <div class="space-y-3">
      <div class="text-xs font-semibold text-slate-300">Стиль и глубина ответов ассистента</div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <!-- Detailed -->
        <div
          @click="form.ai_response_mode = 'detailed'"
          class="p-4 rounded-xl border transition-all cursor-pointer select-none flex flex-col justify-between"
          :class="form.ai_response_mode === 'detailed'
            ? 'bg-indigo-600/20 border-indigo-400/80 shadow-[0_0_20px_rgba(99,102,241,0.25)]'
            : 'bg-[#111a3b]/60 border-[#2d3a63]/40 hover:bg-[#152148]'"
        >
          <div>
            <div class="flex items-center justify-between mb-2">
              <span class="text-xs font-bold text-white">Бизнес-инсайты</span>
              <span v-if="form.ai_response_mode === 'detailed'" class="w-2 h-2 rounded-full bg-indigo-400 shadow-[0_0_8px_rgba(129,140,248,0.8)]"></span>
            </div>
            <p class="text-[11px] text-slate-300 leading-relaxed">
              Развернутые ответы с выявлением узких мест, анализом конверсий и гипотезами по росту выручки.
            </p>
          </div>
          <span class="text-[10px] text-indigo-300 mt-3 font-medium">Рекомендуемый</span>
        </div>

        <!-- Concise -->
        <div
          @click="form.ai_response_mode = 'concise'"
          class="p-4 rounded-xl border transition-all cursor-pointer select-none flex flex-col justify-between"
          :class="form.ai_response_mode === 'concise'
            ? 'bg-indigo-600/20 border-indigo-400/80 shadow-[0_0_20px_rgba(99,102,241,0.25)]'
            : 'bg-[#111a3b]/60 border-[#2d3a63]/40 hover:bg-[#152148]'"
        >
          <div>
            <div class="flex items-center justify-between mb-2">
              <span class="text-xs font-bold text-white">Лаконичный</span>
              <span v-if="form.ai_response_mode === 'concise'" class="w-2 h-2 rounded-full bg-indigo-400 shadow-[0_0_8px_rgba(129,140,248,0.8)]"></span>
            </div>
            <p class="text-[11px] text-slate-300 leading-relaxed">
              Только тезисы и bullet points без вводных фраз. Максимальная скорость сканирования взглядом.
            </p>
          </div>
          <span class="text-[10px] text-slate-400 mt-3">Для быстрых сверок</span>
        </div>

        <!-- Finance / Numbers -->
        <div
          @click="form.ai_response_mode = 'finance'"
          class="p-4 rounded-xl border transition-all cursor-pointer select-none flex flex-col justify-between"
          :class="form.ai_response_mode === 'finance'
            ? 'bg-indigo-600/20 border-indigo-400/80 shadow-[0_0_20px_rgba(99,102,241,0.25)]'
            : 'bg-[#111a3b]/60 border-[#2d3a63]/40 hover:bg-[#152148]'"
        >
          <div>
            <div class="flex items-center justify-between mb-2">
              <span class="text-xs font-bold text-white">Финансовый</span>
              <span v-if="form.ai_response_mode === 'finance'" class="w-2 h-2 rounded-full bg-indigo-400 shadow-[0_0_8px_rgba(129,140,248,0.8)]"></span>
            </div>
            <p class="text-[11px] text-slate-300 leading-relaxed">
              Фокус на цифрах, расчетах маржинальности, отклонениях от плана и табличных сопоставлениях.
            </p>
          </div>
          <span class="text-[10px] text-slate-400 mt-3">Таблицы и метрики</span>
        </div>
      </div>
    </div>

    <!-- Smart Action Suggestions Toggle -->
    <div class="p-4 rounded-xl bg-[#111a3b]/60 border border-[#2d3a63]/40 flex items-center justify-between">
      <div class="pr-4">
        <div class="text-sm font-medium text-white">Умные кнопки дальнейших действий</div>
        <div class="text-xs text-slate-400 mt-0.5">
          Автоматически предлагать кнопки быстрых реакций («Почему?», «Сравнить с прошлым месяцем») под инсайтами
        </div>
      </div>
      <input
        v-model="form.ai_auto_suggest_next_actions"
        type="checkbox"
        class="w-5 h-5 accent-indigo-600 rounded cursor-pointer"
      />
    </div>

    <!-- Save Action -->
    <div class="flex items-center justify-end gap-3 pt-3 border-t border-slate-700/40">
      <span v-if="saveMessage" class="text-xs text-emerald-400 font-medium">
        {{ saveMessage }}
      </span>
      <button
        type="button"
        @click="handleSave"
        :disabled="isSaving"
        class="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium shadow-[0_0_15px_rgba(99,102,241,0.35)] transition-all cursor-pointer disabled:opacity-50"
      >
        {{ isSaving ? 'Сохранение...' : 'Применить настройки' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'
import { useProfile } from '../../composables/useProfile'

const { profile, isSaving, saveMessage, updateProfile } = useProfile()

const form = reactive({
  ai_response_mode: profile.value.ai_response_mode,
  ai_auto_suggest_next_actions: profile.value.ai_auto_suggest_next_actions
})

watch(() => profile.value, (newVal) => {
  form.ai_response_mode = newVal.ai_response_mode
  form.ai_auto_suggest_next_actions = newVal.ai_auto_suggest_next_actions
}, { deep: true })

async function handleSave() {
  await updateProfile(form)
}
</script>
