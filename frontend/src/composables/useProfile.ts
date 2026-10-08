import { ref, watch } from 'vue'
import { api } from './api'
import { sessionVersion } from './session'
import type { Profile, ProfileUpdate } from '../types/platform'

export type UserProfileData = Profile
const empty = (): Profile => ({ id: 0, full_name: '', role: '', department: '', email: '', phone: '', avatar_url: '', timezone: 'Asia/Almaty', notification_preferences: {}, monthly_target: null, monthly_target_formatted: 'План не задан', current_sales: '0.00', current_sales_formatted: 'Нет данных', deals_count: 0, rank_in_team: null, conversion_rate: null, kpi_percent: null, whatsapp_daily_digest: true, whatsapp_stalled_deals: true, whatsapp_critical_kpi: true, ai_response_mode: 'detailed', ai_auto_suggest_next_actions: true, updated_at: '', coverage: { status: 'partial', message: 'Данные не загружены' } })
const profile = ref<Profile>(empty())
const isLoading = ref(false), isSaving = ref(false), saveMessage = ref('')
let revision = 0
watch(sessionVersion, () => {
  revision++
  profile.value = empty()
  saveMessage.value = ''
  isLoading.value = isSaving.value = false
})

export function useProfile() {
  async function fetchProfile() {
    if (isSaving.value) return
    const request = ++revision
    isLoading.value = true
    try {
      const result = await api<Profile>('/profile/')
      if (request === revision) profile.value = result
    } catch (error) {
      if (request === revision) saveMessage.value = error instanceof Error ? error.message : 'Ошибка загрузки'
    } finally {
      if (request === revision) isLoading.value = false
    }
  }

  async function updateProfile(data: ProfileUpdate): Promise<boolean> {
    if (isSaving.value) return false
    const request = ++revision
    isLoading.value = false
    isSaving.value = true
    saveMessage.value = ''
    let body: BodyInit = JSON.stringify(data)
    if (data.avatar) {
      const form = new FormData()
      for (const [key, value] of Object.entries(data)) {
        if (value === undefined) continue
        form.append(key, value instanceof File ? value : typeof value === 'object' ? JSON.stringify(value) : String(value))
      }
      body = form
    }
    try {
      const result = await api<Profile>('/profile/', { method: 'PUT', body })
      if (request !== revision) return false
      profile.value = result
      saveMessage.value = 'Изменения сохранены'
      return true
    } catch (error) {
      if (request === revision) saveMessage.value = error instanceof Error ? error.message : 'Ошибка сохранения'
      return false
    } finally {
      if (request === revision) isSaving.value = false
    }
  }
  return { profile, isLoading, isSaving, saveMessage, fetchProfile, updateProfile }
}
