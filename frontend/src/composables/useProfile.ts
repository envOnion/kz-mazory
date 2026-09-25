import { ref } from 'vue'

const API_BASE = import.meta.env?.VITE_API_URL || '/api'

export interface UserProfileData {
  id: number
  full_name: string
  role: string
  department: string
  email: string
  phone: string
  avatar_url: string
  monthly_target: string
  monthly_target_formatted: string
  current_sales: string
  current_sales_formatted: string
  deals_count: number
  rank_in_team: number
  conversion_rate: string
  kpi_percent: number
  whatsapp_daily_digest: boolean
  whatsapp_stalled_deals: boolean
  whatsapp_critical_kpi: boolean
  ai_response_mode: 'detailed' | 'concise' | 'finance'
  ai_auto_suggest_next_actions: boolean
  updated_at: string
}

const profile = ref<UserProfileData>({
  id: 1,
  full_name: 'Камиль',
  role: 'Ведущий менеджер по продажам',
  department: 'Отдел продаж Aqua Kip',
  email: 'kamil@aquakip.kz',
  phone: '+7 (701) 123-45-67',
  avatar_url: 'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=250&q=80',
  monthly_target: '50000000.00',
  monthly_target_formatted: '50 000 000 ₸',
  current_sales: '31790000.00',
  current_sales_formatted: '31 790 000 ₸',
  deals_count: 15,
  rank_in_team: 1,
  conversion_rate: '35.00',
  kpi_percent: 63.6,
  whatsapp_daily_digest: true,
  whatsapp_stalled_deals: true,
  whatsapp_critical_kpi: true,
  ai_response_mode: 'detailed',
  ai_auto_suggest_next_actions: true,
  updated_at: new Date().toISOString()
})

const isLoading = ref(false)
const isSaving = ref(false)
const saveMessage = ref('')

export function useProfile() {
  async function fetchProfile() {
    isLoading.value = true
    try {
      const token = localStorage.getItem('mazory_access_token')
      const headers: Record<string, string> = {}
      if (token) headers['Authorization'] = `Bearer ${token}`

      const res = await fetch(`${API_BASE}/profile/`, { headers })
      if (res.ok) {
        const data = await res.json()
        profile.value = data
      }
    } catch (e) {
      console.error('Failed to fetch profile:', e)
    } finally {
      isLoading.value = false
    }
  }

  async function updateProfile(partialData: Partial<UserProfileData>): Promise<boolean> {
    isSaving.value = true
    saveMessage.value = ''
    try {
      const token = localStorage.getItem('mazory_access_token')
      const headers: Record<string, string> = {
        'Content-Type': 'application/json'
      }
      if (token) headers['Authorization'] = `Bearer ${token}`

      const res = await fetch(`${API_BASE}/profile/`, {
        method: 'PUT',
        headers,
        body: JSON.stringify(partialData)
      })

      if (res.ok) {
        const data = await res.json()
        if (data.profile) {
          profile.value = data.profile
        }
        saveMessage.value = '✓ Изменения успешно сохранены'
        return true
      }
      return false
    } catch (e) {
      console.error('Failed to update profile:', e)
      return false
    } finally {
      isSaving.value = false
    }
  }

  return {
    profile,
    isLoading,
    isSaving,
    saveMessage,
    fetchProfile,
    updateProfile
  }
}
