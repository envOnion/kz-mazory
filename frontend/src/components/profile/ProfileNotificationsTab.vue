<template>
  <div class="space-y-6">
    <div>
      <h3 class="text-base font-semibold text-white">Каналы связи и уведомления</h3>
      <p class="text-xs text-slate-400 mt-0.5">Управление локальной доставкой отчетов и алертов через WAHA</p>
    </div>

    <!-- Status Banner & Live QR Code for WAHA -->
    <div class="p-4 rounded-2xl bg-[#0e1633]/70 border border-[#2d3a63]/40 space-y-3">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <div
            class="w-2.5 h-2.5 rounded-full shrink-0"
            :class="{
              'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]': wahaStatus === 'WORKING',
              'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)] animate-pulse': wahaStatus === 'SCAN_QR_CODE',
              'bg-slate-500': wahaStatus !== 'WORKING' && wahaStatus !== 'SCAN_QR_CODE'
            }"
          ></div>
          <div>
            <div class="text-xs font-semibold text-white flex items-center gap-2 flex-wrap">
              <span>WAHA (WhatsApp HTTP API)</span>
              <span
                class="px-2 py-0.2 rounded-full text-[10px] font-medium"
                :class="{
                  'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30': wahaStatus === 'WORKING',
                  'bg-amber-500/20 text-amber-300 border border-amber-500/30': wahaStatus === 'SCAN_QR_CODE',
                  'bg-slate-700 text-slate-300': wahaStatus !== 'WORKING' && wahaStatus !== 'SCAN_QR_CODE'
                }"
              >
                {{ wahaStatus === 'WORKING' ? 'Подключен' : wahaStatus === 'SCAN_QR_CODE' ? 'Ожидает скан QR' : wahaStatus }}
              </span>
            </div>
            <div class="text-[11px] text-slate-400 mt-0.5">
              {{ wahaStatus === 'WORKING' ? `Привязан номер: +${wahaUserPhone}` : 'Локальный микросервис без сторонних API (порт 3000)' }}
            </div>
          </div>
        </div>

        <div class="flex items-center gap-2 self-start sm:self-center">
          <button
            type="button"
            @click="toggleQrView"
            class="px-3 py-1.5 rounded-xl bg-cyan-600/25 border border-cyan-400/40 hover:bg-cyan-600 text-white text-xs font-medium transition-all cursor-pointer flex items-center gap-1.5"
          >
            <QrCode class="w-3.5 h-3.5 text-cyan-300" />
            <span>{{ isQrVisible ? 'Скрыть QR' : 'QR-код WhatsApp' }}</span>
          </button>
          <a
            href="http://localhost:3000/dashboard"
            target="_blank"
            class="text-xs text-indigo-400 hover:text-indigo-300 font-medium underline flex items-center gap-1"
            title="Логин: admin / Пароль: mazory2026"
          >
            <span>Панель WAHA</span>
            <ExternalLink class="w-3 h-3" />
          </a>
        </div>
      </div>

      <!-- Live QR Code Card -->
      <div
        v-if="isQrVisible"
        class="mt-3 p-4 rounded-xl bg-[#090f22]/90 border border-cyan-500/30 flex flex-col sm:flex-row items-center gap-5"
      >
        <div class="p-2.5 rounded-xl bg-white shadow-[0_0_25px_rgba(34,211,238,0.35)] shrink-0 flex items-center justify-center">
          <img
            v-if="qrBase64"
            :src="qrBase64"
            alt="WhatsApp QR Code"
            class="w-48 h-48 rounded object-contain"
          />
          <div v-else class="w-48 h-48 flex flex-col items-center justify-center text-slate-700 text-xs text-center p-2">
            <RefreshCw class="w-6 h-6 animate-spin text-cyan-600 mb-2" />
            <span class="font-medium">Генерация QR-кода...</span>
          </div>
        </div>

        <div class="flex-1 space-y-2 text-left">
          <div class="text-xs font-bold text-white flex items-center gap-1.5">
            <Smartphone class="w-4 h-4 text-cyan-400" />
            <span>Как подключить WhatsApp к Mazory:</span>
          </div>
          <ol class="text-[11px] text-slate-300 space-y-1 list-decimal list-inside leading-relaxed">
            <li>Откройте WhatsApp на телефоне</li>
            <li>Перейдите в <strong class="text-white font-semibold">Настройки → Связанные устройства</strong></li>
            <li>Нажмите <strong class="text-white font-semibold">Привязка устройства</strong> и наведите камеру на QR-код</li>
          </ol>
          <p class="text-[10px] text-slate-400 leading-normal pt-1">
            Код WhatsApp Web активен 1-2 минуты. При завершении времени нажмите кнопку обновления.
          </p>
          <div class="pt-1 flex items-center gap-2 flex-wrap">
            <button
              type="button"
              @click="refreshQr"
              :disabled="isLoadingQr"
              class="px-3 py-1.5 rounded-lg bg-cyan-600/30 border border-cyan-400/50 hover:bg-cyan-600/50 text-cyan-200 text-xs font-medium transition-all cursor-pointer flex items-center gap-1.5 disabled:opacity-50"
            >
              <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': isLoadingQr }" />
              <span>Обновить QR-код</span>
            </button>
            <span class="text-[10px] text-slate-500">
              Вход в WAHA Dashboard: admin / mazory2026
            </span>
          </div>
        </div>
      </div>
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
          Адресная отправка уведомления сотруднику (Django Q & Redis)
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
            <span>Продублировать в WhatsApp через WAHA</span>
          </label>

          <button
            type="button"
            @click="handleDispatch"
            :disabled="isDispatching || !dispatchForm.title || !dispatchForm.message"
            class="px-4 py-2 rounded-xl bg-gradient-to-r from-cyan-500 to-indigo-600 hover:from-cyan-400 hover:to-indigo-500 text-white text-xs font-semibold shadow-[0_0_15px_rgba(6,182,212,0.4)] transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed shrink-0 flex items-center gap-1.5 justify-center"
          >
            <Send class="w-3.5 h-3.5" />
            <span>{{ isDispatching ? 'Отправка в очередь...' : 'Отправить через Django Q' }}</span>
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
import { reactive, ref, watch, onMounted, onUnmounted } from 'vue'
import { Send, QrCode, RefreshCw, Smartphone, ExternalLink } from 'lucide-vue-next'
import { useProfile } from '../../composables/useProfile'
import { useNotifications } from '../../composables/useNotifications'

const API_BASE = import.meta.env?.VITE_API_URL || 'http://localhost:8000/api'

const { profile, isSaving, saveMessage, updateProfile } = useProfile()
const { dispatchNotification } = useNotifications()

// WAHA QR & Connection State
const wahaStatus = ref('SCAN_QR_CODE')
const wahaUserPhone = ref('')
const isQrVisible = ref(false)
const qrBase64 = ref('')
const isLoadingQr = ref(false)
let wahaPollingInterval: number | null = null

async function checkWahaStatus() {
  try {
    const res = await fetch(`${API_BASE}/whatsapp/status/`)
    if (res.ok) {
      const data = await res.json()
      wahaStatus.value = data.status || 'STOPPED'
      if (data.me?.id) {
        wahaUserPhone.value = data.me.id.split('@')[0]
      }
    }
  } catch (e) {
    console.error('Failed to check WAHA status:', e)
  }
}

async function refreshQr() {
  isLoadingQr.value = true
  try {
    const res = await fetch(`${API_BASE}/whatsapp/qr/?format=json`)
    if (res.ok) {
      const data = await res.json()
      qrBase64.value = data.qr_base64
      wahaStatus.value = data.status
    }
  } catch (e) {
    console.error('Failed to fetch WAHA QR:', e)
  } finally {
    isLoadingQr.value = false
  }
}

function toggleQrView() {
  isQrVisible.value = !isQrVisible.value
  if (isQrVisible.value && !qrBase64.value) {
    refreshQr()
  }
}

onMounted(() => {
  checkWahaStatus()
  wahaPollingInterval = window.setInterval(checkWahaStatus, 4000)
})

onUnmounted(() => {
  if (wahaPollingInterval) clearInterval(wahaPollingInterval)
})

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
      dispatchStatusMessage.value = `✓ Уведомление доставлено в очередь Django Q & Redis для ${dispatchForm.phone || profile.value.phone}`
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

