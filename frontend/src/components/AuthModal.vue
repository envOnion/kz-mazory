<template>
  <transition
    enter-active-class="transition duration-200 ease-out"
    enter-from-class="opacity-0 scale-95"
    enter-to-class="opacity-100 scale-100"
    leave-active-class="transition duration-150 ease-in"
    leave-from-class="opacity-100 scale-100"
    leave-to-class="opacity-0 scale-95"
  >
    <div
      v-if="isOpen"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-md select-none"
      @click.self="$emit('close')"
    >
      <div class="relative w-full max-w-md p-6 sm:p-8 rounded-3xl bg-[#0c142c]/95 border border-indigo-500/40 shadow-[0_16px_50px_rgba(0,0,0,0.6),0_0_30px_rgba(99,102,241,0.25)] text-slate-100">
        <!-- Close button -->
        <button
          type="button"
          @click="$emit('close')"
          class="absolute top-5 right-5 p-1.5 text-slate-400 hover:text-white rounded-full hover:bg-white/10 transition-colors"
        >
          <X class="w-5 h-5" />
        </button>

        <!-- Step 1: Phone input -->
        <div v-if="step === 'phone'" class="flex flex-col items-center text-center">
          <div class="relative mb-4 flex items-center justify-center">
            <div class="absolute inset-0 bg-indigo-500/20 blur-2xl rounded-full scale-125"></div>
            <MazoryLogo :size="48" :glow="true" />
          </div>

          <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">
            Вход в Mazory
          </h2>
          <p class="text-xs sm:text-sm text-slate-400 mt-1.5 mb-6 max-w-xs">
            Введите номер телефона для получения одноразового кода авторизации
          </p>

          <form @submit.prevent="handleSendCode" class="w-full space-y-4">
            <div class="relative">
              <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-slate-400">
                <Phone class="w-4 h-4 text-indigo-400" />
              </div>
              <input
                ref="phoneInputRef"
                v-model="phone"
                type="tel"
                placeholder="+7 (999) 000-00-00"
                required
                class="w-full pl-10 pr-4 py-3 rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white placeholder-slate-500 text-sm focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.3)] transition-all tracking-wide"
              />
            </div>

            <!-- Error message banner -->
            <div v-if="authError" class="p-2.5 rounded-lg bg-rose-500/15 border border-rose-500/40 text-xs text-rose-300 text-left">
              {{ authError }}
            </div>

            <button
              type="submit"
              :disabled="isSendingCode || !phone.trim()"
              class="w-full py-3 rounded-xl font-medium text-sm text-white bg-indigo-600 hover:bg-indigo-500 border border-indigo-400/50 shadow-[0_0_20px_rgba(99,102,241,0.4)] disabled:opacity-50 disabled:pointer-events-none transition-all cursor-pointer flex items-center justify-center gap-2"
            >
              <span v-if="isSendingCode" class="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></span>
              <span>{{ isSendingCode ? 'Отправка...' : 'Получить СМС-код' }}</span>
              <ArrowRight v-if="!isSendingCode" class="w-4 h-4" />
            </button>
          </form>

          <p class="text-[11px] text-slate-500 mt-5">
            Сервис защищен сквозным шифрованием и локальной очередью Redis
          </p>
        </div>

        <!-- Step 2: OTP Verification -->
        <div v-else class="flex flex-col items-center text-center">
          <div class="w-12 h-12 rounded-2xl bg-indigo-500/20 border border-indigo-400/40 flex items-center justify-center text-indigo-300 mb-4">
            <Lock class="w-6 h-6 text-indigo-400" />
          </div>

          <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">
            Код подтверждения
          </h2>
          <p class="text-xs sm:text-sm text-slate-400 mt-1.5 mb-6 max-w-xs">
            Код отправлен на номер <span class="text-indigo-200 font-semibold">{{ phone }}</span>
          </p>

          <form @submit.prevent="handleVerifyCode" class="w-full space-y-4">
            <div class="relative">
              <input
                ref="codeInputRef"
                v-model="code"
                type="text"
                maxlength="6"
                placeholder="• • • •"
                required
                class="w-full py-3 text-center text-2xl font-mono tracking-[0.5em] rounded-xl bg-[#121c3b]/80 border border-[#354676]/60 text-white placeholder-slate-600 focus:outline-none focus:border-indigo-400 focus:shadow-[0_0_15px_rgba(99,102,241,0.3)] transition-all"
              />
            </div>

            <!-- Error message banner -->
            <div v-if="authError" class="p-2.5 rounded-lg bg-rose-500/15 border border-rose-500/40 text-xs text-rose-300 text-left">
              {{ authError }}
            </div>

            <button
              type="submit"
              :disabled="isVerifying || !code.trim()"
              class="w-full py-3 rounded-xl font-medium text-sm text-white bg-indigo-600 hover:bg-indigo-500 border border-indigo-400/50 shadow-[0_0_20px_rgba(99,102,241,0.4)] disabled:opacity-50 disabled:pointer-events-none transition-all cursor-pointer flex items-center justify-center gap-2"
            >
              <span v-if="isVerifying" class="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></span>
              <span>{{ isVerifying ? 'Проверка...' : 'Войти в систему' }}</span>
            </button>
          </form>

          <!-- Resend countdown & change phone -->
          <div class="flex items-center justify-between w-full text-xs text-slate-400 mt-5 pt-3 border-t border-slate-700/40">
            <button
              type="button"
              @click="step = 'phone'"
              class="hover:text-indigo-300 transition-colors"
            >
              ← Изменить номер
            </button>

            <span v-if="cooldownSeconds > 0" class="text-slate-500">
              Повторно через {{ cooldownSeconds }} сек.
            </span>
            <button
              v-else
              type="button"
              @click="handleSendCode"
              class="text-indigo-400 hover:text-indigo-300 font-medium transition-colors cursor-pointer"
            >
              Отправить снова
            </button>
          </div>
        </div>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { X, Phone, ArrowRight, Lock } from 'lucide-vue-next'
import MazoryLogo from './icons/MazoryLogo.vue'
import { useAuth } from '../composables/useAuth'

const props = defineProps<{
  isOpen: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'success'): void
}>()

const {
  isSendingCode,
  isVerifying,
  authError,
  cooldownSeconds,
  sendVerificationCode,
  verifyCode
} = useAuth()

const step = ref<'phone' | 'code'>('phone')
const phone = ref('+7 (701) 987-65-43')
const code = ref('')
const phoneInputRef = ref<HTMLInputElement | null>(null)
const codeInputRef = ref<HTMLInputElement | null>(null)

watch(() => props.isOpen, (newVal) => {
  if (newVal) {
    step.value = 'phone'
    code.value = ''
    authError.value = ''
    nextTick(() => phoneInputRef.value?.focus())
  }
})

async function handleSendCode() {
  if (!phone.value.trim()) return
  const ok = await sendVerificationCode(phone.value)
  if (ok) {
    step.value = 'code'
    code.value = ''
    nextTick(() => codeInputRef.value?.focus())
  }
}

async function handleVerifyCode() {
  if (!code.value.trim()) return
  const ok = await verifyCode(phone.value, code.value)
  if (ok) {
    emit('success')
  }
}
</script>
