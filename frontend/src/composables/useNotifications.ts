import { ref, watch } from 'vue'
import { api, post } from './api'
import { currentUser, sessionVersion } from './session'
import type { NotificationItem, DispatchNotificationPayload } from '../types/platform'
export type { NotificationItem, DispatchNotificationPayload } from '../types/platform'
const notifications = ref<NotificationItem[]>([]), unreadCount = ref(0), isLoading = ref(false), isPopoverOpen = ref(false)
function clearNotifications() { notifications.value = []; unreadCount.value = 0; isPopoverOpen.value = false }
watch(sessionVersion, clearNotifications)
export function useNotifications() {
  async function fetchNotifications() {
    if (!currentUser.value) return clearNotifications()
    isLoading.value = true
    try { const data = await api<{ notifications: NotificationItem[]; unread_count: number }>('/notifications/'); notifications.value = data.notifications; unreadCount.value = data.unread_count }
    catch { clearNotifications() } finally { isLoading.value = false }
  }
  async function markAllAsRead() { await post('/notifications/read-all/', {}); await fetchNotifications() }
  async function dispatchNotification(payload: DispatchNotificationPayload) { return post('/notifications/dispatch/', { ...payload, idempotency_key: crypto.randomUUID() }) }
  async function acknowledge(id: number) { await post(`/notifications/${id}/ack/`, {}); await fetchNotifications() }
  function togglePopover() { isPopoverOpen.value = !isPopoverOpen.value; if (isPopoverOpen.value) void fetchNotifications() }
  return { notifications, unreadCount, isLoading, isPopoverOpen, fetchNotifications, markAllAsRead, dispatchNotification, acknowledge, clearNotifications, togglePopover, closePopover: () => { isPopoverOpen.value = false } }
}
