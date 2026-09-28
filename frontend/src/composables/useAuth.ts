import { ref, computed } from 'vue'
import { API_BASE, currentUser, accessToken, acceptSession, refreshSession, logoutSession, clearSession } from './session'
import type { AuthResponse, ApiError } from '../types/platform'
const isAuthModalOpen = ref(false), isSendingCode = ref(false), isVerifying = ref(false)
const authError = ref(''), cooldownSeconds = ref(0)
let timer: ReturnType<typeof setInterval> | undefined
export function useAuth() {
  function startCooldown(seconds: number) {
    cooldownSeconds.value = seconds
    clearInterval(timer)
    timer = setInterval(() => { if (cooldownSeconds.value > 0) cooldownSeconds.value--; else clearInterval(timer) }, 1000)
  }
  async function sendVerificationCode(phone: string) {
    isSendingCode.value = true; authError.value = ''
    try {
      const res = await fetch(`${API_BASE}/auth/send-code/`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ phone }) })
      const data: ApiError & { cooldown?: number } = await res.json()
      if (!res.ok) throw new Error(data.error || 'Не удалось отправить код')
      startCooldown(data.cooldown || 45); return true
    } catch (error) { authError.value = error instanceof Error ? error.message : 'Ошибка связи'; return false }
    finally { isSendingCode.value = false }
  }
  async function verifyCode(phone: string, code: string) {
    isVerifying.value = true; authError.value = ''
    try {
      const res = await fetch(`${API_BASE}/auth/verify-code/`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ phone, code }) })
      if (!res.ok) { const error: ApiError = await res.json(); throw new Error(error.error || 'Код недействителен') }
      const data: AuthResponse = await res.json()
      clearSession(); acceptSession(data); isAuthModalOpen.value = false; return true
    } catch (error) { authError.value = error instanceof Error ? error.message : 'Ошибка связи'; return false }
    finally { isVerifying.value = false }
  }
  return { isAuthenticated: computed(() => Boolean(accessToken.value && currentUser.value)), currentUser, isAuthModalOpen,
    isSendingCode, isVerifying, authError, cooldownSeconds, checkAuth: refreshSession, sendVerificationCode, verifyCode, logout: logoutSession }
}
