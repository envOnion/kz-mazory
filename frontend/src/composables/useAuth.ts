import { ref } from 'vue'

const API_BASE = import.meta.env?.VITE_API_URL || '/api'

export interface UserProfile {
  id: number
  phone: string
  name: string
  username: string
}

const isAuthenticated = ref(false)
const currentUser = ref<UserProfile | null>(null)
const isAuthModalOpen = ref(false)
const isSendingCode = ref(false)
const isVerifying = ref(false)
const authError = ref('')
const cooldownSeconds = ref(0)
let timerInterval: number | null = null

export function useAuth() {
  function startCooldown(seconds: number) {
    cooldownSeconds.value = seconds
    if (timerInterval) clearInterval(timerInterval)
    timerInterval = window.setInterval(() => {
      if (cooldownSeconds.value > 0) {
        cooldownSeconds.value--
      } else {
        if (timerInterval) clearInterval(timerInterval)
      }
    }, 1000)
  }

  async function checkAuth() {
    const token = localStorage.getItem('mazory_access_token')
    if (!token) {
      isAuthenticated.value = false
      currentUser.value = null
      return
    }

    try {
      const res = await fetch(`${API_BASE}/auth/me/`, {
        headers: {
          Authorization: `Bearer ${token}`
        }
      })
      if (res.ok) {
        const data = await res.json()
        currentUser.value = data
        isAuthenticated.value = true
      } else {
        logout()
      }
    } catch {
      // Offline / server waking up: keep local session if cached
      const cached = localStorage.getItem('mazory_user')
      if (cached) {
        currentUser.value = JSON.parse(cached)
        isAuthenticated.value = true
      }
    }
  }

  async function sendVerificationCode(phone: string): Promise<boolean> {
    isSendingCode.value = true
    authError.value = ''
    try {
      const res = await fetch(`${API_BASE}/auth/send-code/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone })
      })
      const data = await res.json()
      if (res.ok) {
        startCooldown(data.cooldown || 45)
        return true
      } else {
        authError.value = data.error || 'Ошибка отправки кода'
        return false
      }
    } catch {
      authError.value = 'Не удалось связаться с сервером бэкенда'
      return false
    } finally {
      isSendingCode.value = false
    }
  }

  async function verifyCode(phone: string, code: string): Promise<boolean> {
    isVerifying.value = true
    authError.value = ''
    try {
      const res = await fetch(`${API_BASE}/auth/verify-code/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, code })
      })
      const data = await res.json()
      if (res.ok) {
        localStorage.setItem('mazory_access_token', data.access)
        localStorage.setItem('mazory_refresh_token', data.refresh)
        localStorage.setItem('mazory_user', JSON.stringify(data.user))
        currentUser.value = data.user
        isAuthenticated.value = true
        isAuthModalOpen.value = false
        return true
      } else {
        authError.value = data.error || 'Неверный код'
        return false
      }
    } catch {
      authError.value = 'Ошибка проверки кода'
      return false
    } finally {
      isVerifying.value = false
    }
  }

  function logout() {
    localStorage.removeItem('mazory_access_token')
    localStorage.removeItem('mazory_refresh_token')
    localStorage.removeItem('mazory_user')
    isAuthenticated.value = false
    currentUser.value = null
  }

  async function sendWhatsAppAlert(phone: string, message: string) {
    try {
      const token = localStorage.getItem('mazory_access_token')
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (token) {
        headers['Authorization'] = `Bearer ${token}`
      }
      const res = await fetch(`${API_BASE}/whatsapp/send/`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ phone, message })
      })
      return await res.json()
    } catch (e) {
      console.error('Failed to dispatch WhatsApp message:', e)
      return { status: 'error', detail: String(e) }
    }
  }

  return {
    isAuthenticated,
    currentUser,
    isAuthModalOpen,
    isSendingCode,
    isVerifying,
    authError,
    cooldownSeconds,
    checkAuth,
    sendVerificationCode,
    verifyCode,
    logout,
    sendWhatsAppAlert
  }
}
