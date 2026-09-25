<template>
  <div class="space-y-6">
    <div>
      <h3 class="text-base font-semibold text-white">Каналы связи и уведомления</h3>
      <p class="text-xs text-slate-400 mt-0.5">Настройка персональных алертов и сводок в WhatsApp</p>
    </div>

    <!-- Toggles List -->
    <div class="space-y-3">
      <!-- Toggle 1: Daily Digest -->
      <label class="flex items-center justify-between p-4 rounded-xl bg-[#111a3b]/60 border border-[#2d3a63]/40 cursor-pointer hover:bg-[#142048]/70 transition-colors">
        <div class="pr-4">
          <div class="text-sm font-medium text-white">Утренний AI-дайджест в WhatsApp (09:00)</div>
          <div class="text-xs text-slate-400 mt-0.5">Ежедневная сводка по задачам дня, горячим лидам и плану продаж</div>
        </div>
        <input
          v-model="form.whatsapp_daily_digest"
          type="checkbox"
          class="w-5 h-5 accent-indigo-600 rounded cursor-pointer"
        />
      </label>

      <!-- Toggle 2: Stalled Deals Alert -->
      <label class="flex items-center justify-between p-4 rounded-xl bg-[#111a3b]/60 border border-[#2d3a63]/40 cursor-pointer hover:bg-[#142048]/70 transition-colors">
        <div class="pr-4">
          <div class="text-sm font-medium text-white">Оповещения о зависших сделках</div>
          <div class="text-xs text-slate-400 mt-0.5">Уведомлять, если по клиенту на стадии КП нет контакта более 3 дней</div>
        </div>
        <input
          v-model="form.whatsapp_stalled_deals"
          type="checkbox"
          class="w-5 h-5 accent-indigo-600 rounded cursor-pointer"
        />
      </label>

      <!-- Toggle 3: KPI Achievements -->
      <label class="flex items-center justify-between p-4 rounded-xl bg-[#111a3b]/60 border border-[#2d3a63]/40 cursor-pointer hover:bg-[#142048]/70 transition-colors">
        <div class="pr-4">
          <div class="text-sm font-medium text-white">Уведомления о выполнении плана и рекордах</div>
          <div class="text-xs text-slate-400 mt-0.5">Сообщения при достижении 50%, 80%, 100% месячного таргета</div>
        </div>
        <input
          v-model="form.whatsapp_critical_kpi"
          type="checkbox"
          class="w-5 h-5 accent-indigo-600 rounded cursor-pointer"
        />
      </label>
    </div>

    <!-- Targeted Dispatch Form: Send Real Notifications to Specific Users -->
    <div class="p-5 rounded-2xl bg-[#0e1633]/70 border border-indigo-500/30 space-y-4">
      <div class="flex items-center gap-2">
        <Send class="w-4 h-4 text-cyan-400" />
        <h4 class="text-xs font-bold text-white uppercase tracking-wider">
          Адресная отправка уведомления сотруднику
        </h4>
      </div>
      <p class="text-[11px] text-slate-400 leading-relaxed">
        Отправляет реальное бизнес-событие в персональную очередь конкретного сотрудника. При входе под этим номером уведомление отобразится в его колокольчике.
      </p>

      <div class="space-y-3">
        <!-- Target Phone -->
        <div>
          <label class="block text-[11px] font-medium text-slate-300 mb-1">
            Номер телефона получателя
          </label>
          <input
            v-model="dispatchForm.phone"
            type="text"
            placeholder="+7 701 987 65 43 (или 'all' для всех сотрудников)"
            class="w-full px-3.5 py-2 rounded-xl bg-[#090f22]/80 border border-[#2d3a63] text-white text-xs placeholder:text-slate-600 focus:outline-none focus:border-indigo-500/80 transition-colors"
          />
        </div>

        <!-- Quick Presets -->
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="text-[10px] text-slate-400">Быстрый шаблон:</span>
          <button
            type="button"
            @click="applyPreset('deal')"
            class="px-2 py-0.5 rounded-lg bg-cyan-950/60 border border-cyan-500/40 text-cyan-300 text-[10px] hover:bg-cyan-900/60 cursor-pointer"
          >
            Сделка
          </button>
          <button
            type="button"
            @click="applyPreset('urgent')"
            class="px-2 py-0.5 rounded-lg bg-rose-950/60 border border-rose-500/40 text-rose-300 text-[10px] hover:bg-rose-900/60 cursor-pointer"
          >
            Срочно
          </button>
          <button
            type="button"
            @click="applyPreset('kpi')"
            class="px-2 py-0.5 rounded-lg bg-emerald-950/60 border border-emerald-500/40 text-emerald-300 text-[10px] hover:bg-emerald-900/60 cursor-pointer"
          >
            KPI рекорд
          </button>
        </div>

        <!-- Title & Type -->
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div class="sm:col-span-2">
            <label class="block text-[11px] font-medium text-slate-300 mb-1">
              Заголовок события
            </label>
            <input
              v-model="dispatchForm.title"
              type="text"
              placeholder="Например: Срочная сделка: ТОО Атырау Энерго"
              class="w-full px-3.5 py-2 rounded-xl bg-[#090f22]/80 border border-[#2d3a63] text-white text-xs placeholder:text-slate-600 focus:outline-none focus:border-indigo-500/80 transition-colors"
            />
          </div>

          <div>
            <label class="block text-[11px] font-medium text-slate-300 mb-1">
              Тип события
            </label>
            <select
              v-model="dispatchForm.type"
              class="w-full px-3.5 py-2 rounded-xl bg-[#090f22]/80 border border-[#2d3a63] text-white text-xs focus:outline-none focus:border-indigo-500/80 transition-colors cursor-pointer"
            >
              <option value="deal">Сделка</option>
              <option value="urgent">Срочно</option>
              <option value="warning">Внимание</option>
              <option value="kpi">KPI рекорд</option>
              <option value="info">Инфо</option>
            </select>
          </div>
        </div>

        <!-- Message -->
        <div>
          <label class="block text-[11px] font-medium text-slate-300 mb-1">
            Текст уведомления
          </label>
          <textarea
            v-model="dispatchForm.message"
            rows="2"
            placeholder="Описание события для сотрудника..."
            class="w-full px-3.5 py-2 rounded-xl bg-[#090f22]/80 border border-[#2d3a63] text-white text-xs placeholder:text-slate-600 focus:outline-none focus:border-indigo-500/80 transition-colors resize-none"
          ></textarea>
        </div>

        <!-- WhatsApp Duplicate Toggle & Submit -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-2">
          <label class="flex items-center gap-2 text-xs text-slate-300 cursor-pointer select-none">
            <input
              v-model="dispatchForm.send_whatsapp"
              type="checkbox"
              class="w-4 h-4 accent-emerald-500 rounded cursor-pointer"
            />
            <span>Продублировать в WhatsApp</span>
          </label>

          <button
            type="button"
            @click="handleDispatch"
            :disabled="isDispatching || !dispatchForm.title || !dispatchForm.message"
            class="px-4 py-2 rounded-xl bg-gradient-to-r from-cyan-500 to-indigo-600 hover:from-cyan-400 hover:to-indigo-500 text-white text-xs font-semibold shadow-[0_0_15px_rgba(6,182,212,0.4)] transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed shrink-0 flex items-center gap-1.5 justify-center"
          >
            <Send class="w-3.5 h-3.5" />
            <span>{{ isDispatching ? 'Отправка...' : 'Отправить уведомление' }}</span>
          </button>
        </div>

        <!-- Feedback message -->
        <div
          v-if="dispatchStatusMessage"
          class="p-3 rounded-xl text-xs flex items-center gap-2"
          :class="dispatchStatusType === 'success' ? 'bg-emerald-500/15 border border-emerald-500/40 text-emerald-300' : 'bg-rose-500/15 border border-rose-500/40 text-rose-300'"
        >
          <span>{{ dispatchStatusMessage }}</span>
        </div>
      </div>
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
        {{ isSaving ? 'Сохранение...' : 'Сохранить настройки' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { Send } from 'lucide-vue-next'
import { useProfile } from '../../composables/useProfile'
import { useNotifications } from '../../composables/useNotifications'

const { profile, isSaving, saveMessage, updateProfile } = useProfile()
const { dispatchNotification } = useNotifications()

const form = reactive({
  whatsapp_daily_digest: profile.value.whatsapp_daily_digest,
  whatsapp_stalled_deals: profile.value.whatsapp_stalled_deals,
  whatsapp_critical_kpi: profile.value.whatsapp_critical_kpi
})

watch(() => profile.value, (newVal) => {
  form.whatsapp_daily_digest = newVal.whatsapp_daily_digest
  form.whatsapp_stalled_deals = newVal.whatsapp_stalled_deals
  form.whatsapp_critical_kpi = newVal.whatsapp_critical_kpi
  if (!dispatchForm.phone) {
    dispatchForm.phone = newVal.phone
  }
}, { deep: true })


async function handleSave() {
  await updateProfile(form)
}

// Targeted Dispatch Form State
const isDispatching = ref(false)
const dispatchStatusMessage = ref('')
const dispatchStatusType = ref<'success' | 'error'>('success')

const dispatchForm = reactive<{
  phone: string
  title: string
  message: string
  type: 'deal' | 'urgent' | 'warning' | 'kpi' | 'info'
  send_whatsapp: boolean
}>({
  phone: profile.value.phone || '',
  title: '',
  message: '',
  type: 'deal',
  send_whatsapp: true
})

function applyPreset(preset: 'deal' | 'urgent' | 'kpi') {
  if (preset === 'deal') {
    dispatchForm.title = 'Срочная сделка: ТОО Атырау Энерго'
    dispatchForm.message = 'Клиент подтвердил спецификацию. Срочно выставить счет до 18:00.'
    dispatchForm.type = 'deal'
  } else if (preset === 'urgent') {
    dispatchForm.title = 'Требуется внимание: Зависшая сделка'
    dispatchForm.message = 'По клиенту ТОО Восток-Пром нет контакта 3 дня на этапе КП.'
    dispatchForm.type = 'urgent'
  } else if (preset === 'kpi') {
    dispatchForm.title = 'Новый рекорд KPI команды'
    dispatchForm.message = 'План отдела продаж перевыполнен на 108%. Поздравляем команду!'
    dispatchForm.type = 'kpi'
  }
}

async function handleDispatch() {
  isDispatching.value = true
  dispatchStatusMessage.value = ''
  try {
    const res = await dispatchNotification({
      phone: dispatchForm.phone || profile.value.phone,
      title: dispatchForm.title,
      message: dispatchForm.message,
      type: dispatchForm.type,
      send_whatsapp: dispatchForm.send_whatsapp
    })

    if (res.status === 'success') {
      dispatchStatusType.value = 'success'
      dispatchStatusMessage.value = `✓ Уведомление успешно отправлено для ${dispatchForm.phone || profile.value.phone}`
      dispatchForm.title = ''
      dispatchForm.message = ''
    } else {
      dispatchStatusType.value = 'error'
      dispatchStatusMessage.value = `Ошибка: ${res.error || 'Не удалось отправить'}`
    }
  } finally {
    isDispatching.value = false
  }
}
</script>

