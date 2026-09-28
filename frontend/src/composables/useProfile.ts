import { ref, watch } from 'vue'
import { api } from './api'
import { sessionVersion } from './session'
import type { Profile } from '../types/platform'
export type UserProfileData = Profile
const empty = (): Profile => ({ id: 0, full_name: '', role: '', department: '', email: '', phone: '', avatar_url: '', timezone: 'Asia/Almaty', notification_preferences: {}, monthly_target: null, monthly_target_formatted: 'План не задан', current_sales: '0.00', current_sales_formatted: 'Нет данных', deals_count: 0, rank_in_team: null, conversion_rate: null, kpi_percent: null, whatsapp_daily_digest: true, whatsapp_stalled_deals: true, whatsapp_critical_kpi: true, ai_response_mode: 'detailed', ai_auto_suggest_next_actions: true, updated_at: '', coverage: { status: 'partial', message: 'Данные не загружены' } })
const profile = ref<Profile>(empty()), isLoading = ref(false), isSaving = ref(false), saveMessage = ref('')
watch(sessionVersion, () => { profile.value = empty(); saveMessage.value = '' })
export function useProfile() {
  async function fetchProfile() {
    isLoading.value = true
    try { profile.value = await api<Profile>('/profile/') } catch (error) { saveMessage.value = error instanceof Error ? error.message : 'Ошибка загрузки' }
    finally { isLoading.value = false }
  }
  async function updateProfile(data: Partial<Profile>): Promise<boolean> {
    isSaving.value = true; saveMessage.value = ''
    try { profile.value = await api<Profile>('/profile/', { method: 'PUT', body: JSON.stringify(data) }); saveMessage.value = 'Изменения сохранены'; return true }
    catch (error) { saveMessage.value = error instanceof Error ? error.message : 'Ошибка сохранения'; return false }
    finally { isSaving.value = false }
  }
  return { profile, isLoading, isSaving, saveMessage, fetchProfile, updateProfile }
}
