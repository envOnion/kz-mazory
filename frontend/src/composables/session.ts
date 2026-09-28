import { ref } from 'vue'
import type { AuthUser, AuthResponse } from '../types/platform'
export const API_BASE = import.meta.env.VITE_API_URL || '/api'
export const currentUser = ref<AuthUser | null>(null)
export const sessionVersion = ref(0)
export const accessToken = ref('')
let csrf = ''
let hasSessionCookie = false
let refreshPromise: Promise<boolean> | null = null

export function clearSession() {
  accessToken.value = ''
  currentUser.value = null
  sessionVersion.value++
  // Remove credentials left by older releases; new credentials stay in memory/cookies.
  for (const key of ['mazory_access_token', 'mazory_refresh_token', 'mazory_user']) localStorage.removeItem(key)
}
export function acceptSession(data: AuthResponse) {
  if (currentUser.value?.id !== data.user.id) sessionVersion.value++
  accessToken.value = data.access
  currentUser.value = data.user
  csrf = data.csrf_token
}
export async function csrfToken(): Promise<string> {
  const response = await fetch(`${API_BASE}/auth/refresh/`, { credentials: 'include' })
  if (!response.ok) throw new Error('Не удалось связаться с сервером')
  const data: { csrf_token: string; has_session: boolean } = await response.json()
  csrf = data.csrf_token
  hasSessionCookie = data.has_session
  return csrf
}
export async function refreshSession(): Promise<boolean> {
  if (refreshPromise) return refreshPromise
  const run = async () => {
    const generation = sessionVersion.value
    try {
      await csrfToken()
      if (!hasSessionCookie) return false
      const response = await fetch(`${API_BASE}/auth/refresh/`, { method: 'POST', credentials: 'include', headers: { 'X-CSRFToken': csrf } })
      if (!response.ok) throw new Error('Требуется вход')
      const data: AuthResponse = await response.json()
      if (generation !== sessionVersion.value) return false
      acceptSession(data)
      return true
    } catch {
      if (generation === sessionVersion.value) clearSession()
      return false
    }
  }
  const pending: Promise<boolean> = Promise.resolve(navigator.locks ? navigator.locks.request('mazory-refresh', run) : run()).then(value => value)
  refreshPromise = pending.finally(() => { refreshPromise = null })
  return pending
}
export async function logoutSession() {
  try {
    await csrfToken()
    await fetch(`${API_BASE}/auth/logout/`, { method: 'POST', credentials: 'include', headers: { 'X-CSRFToken': csrf } })
  } finally { clearSession() }
}
