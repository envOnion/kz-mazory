import { ref } from 'vue'

const API_BASE = import.meta.env?.VITE_API_URL || 'http://localhost:8000/api'

export interface NotificationItem {
  id: string
  title: string
  message: string
  type: 'warning' | 'success' | 'info' | 'urgent' | 'deal' | 'kpi'
  time: string
  is_read: boolean
  created_at?: number
}

export interface DispatchNotificationPayload {
  phone?: string
  phones?: string[]
  title: string
  message: string
  type?: 'warning' | 'success' | 'info' | 'urgent' | 'deal' | 'kpi'
  send_whatsapp?: boolean
}

const notifications = ref<NotificationItem[]>([])
const unreadCount = ref(0)
const isLoading = ref(false)
const isPopoverOpen = ref(false)

function getAuthHeaders(): Record<string, string> | null {
  const token = localStorage.getItem('mazory_access_token')
  if (!token) return null
  return {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`
  }
}

export function useNotifications() {
  async function fetchNotifications() {
    const headers = getAuthHeaders()
    if (!headers) {
      notifications.value = []
      unreadCount.value = 0
      return
    }

    isLoading.value = true
    try {
      const res = await fetch(`${API_BASE}/notifications/`, {
        headers
      })
      if (res.ok) {
        const data = await res.json()
        notifications.value = data.notifications || []
        unreadCount.value = data.unread_count || 0
      } else if (res.status === 401) {
        clearNotifications()
      }
    } catch (e) {
      console.error('Failed to fetch notifications from Redis:', e)
    } finally {
      isLoading.value = false
    }
  }

  async function markAllAsRead() {
    const headers = getAuthHeaders()
    if (!headers) return

    try {
      const res = await fetch(`${API_BASE}/notifications/read-all/`, {
        method: 'POST',
        headers,
        body: JSON.stringify({})
      })
      if (res.ok) {
        const data = await res.json()
        notifications.value = data.notifications || []
        unreadCount.value = 0
      }
    } catch (e) {
      console.error('Failed to mark notifications as read:', e)
    }
  }

  async function dispatchNotification(payload: DispatchNotificationPayload) {
    const headers = getAuthHeaders()
    if (!headers) {
      return { status: 'error', error: 'Требуется авторизация' }
    }

    try {
      const res = await fetch(`${API_BASE}/notifications/dispatch/`, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload)
      })
      const data = await res.json()
      if (res.ok) {
        // Refresh notifications after a short delay so Redis/Django Q processes it
        setTimeout(() => {
          fetchNotifications()
        }, 600)
        return { status: 'success', data }
      } else {
        return { status: 'error', error: data.error || 'Ошибка отправки' }
      }
    } catch (e) {
      return { status: 'error', error: String(e) }
    }
  }

  function clearNotifications() {
    notifications.value = []
    unreadCount.value = 0
    isPopoverOpen.value = false
  }

  function togglePopover() {
    isPopoverOpen.value = !isPopoverOpen.value
    if (isPopoverOpen.value) {
      fetchNotifications()
    }
  }

  function closePopover() {
    isPopoverOpen.value = false
  }

  return {
    notifications,
    unreadCount,
    isLoading,
    isPopoverOpen,
    fetchNotifications,
    markAllAsRead,
    dispatchNotification,
    clearNotifications,
    togglePopover,
    closePopover
  }
}

