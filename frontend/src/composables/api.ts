import { API_BASE, accessToken, sessionVersion, refreshSession, clearSession } from './session'
import type { ApiError, Operation } from '../types/platform'

export async function api<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const generation = sessionVersion.value
  const headers = new Headers(init.headers)
  if (accessToken.value) headers.set('Authorization', `Bearer ${accessToken.value}`)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' })
  if (generation !== sessionVersion.value) throw new Error('Сессия изменилась')
  if (response.status === 401 && retry) {
    if (await refreshSession()) return api<T>(path, init, false)
    clearSession()
    throw new Error('Войдите в систему')
  }
  if (!response.ok) {
    const error: ApiError = await response.json().catch(() => ({}))
    throw new Error(error.error || `Ошибка запроса (${response.status})`)
  }
  if (response.status === 204) return undefined as T
  const data: T = await response.json()
  if (generation !== sessionVersion.value) throw new Error('Сессия изменилась')
  return data
}
export function post<T>(path: string, data: object): Promise<T> { return api<T>(path, { method: 'POST', body: JSON.stringify(data) }) }
export async function pollOperation<T>(id: number, signal?: AbortSignal): Promise<T> {
  for (let attempt = 0; attempt < 180; attempt++) {
    if (signal?.aborted) throw new Error('Запрос отменён')
    const operation = await api<Operation<T>>(`/operations/${id}/`, { signal })
    if (operation.status === 'succeeded' && operation.result !== null) return operation.result
    if (['failed', 'expired', 'cancelled'].includes(operation.status)) throw new Error(operation.status === 'failed' ? 'Не удалось обработать запрос. Попробуйте позже; KPI и кабинет доступны.' : 'Запрос отменён или доступ изменился')
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('Обработка занимает больше времени. Запрос можно повторить позже.')
}
export async function download(path: string, name: string): Promise<void> {
  const generation = sessionVersion.value
  const response = await fetch(`${API_BASE}${path}`, { headers: { Authorization: `Bearer ${accessToken.value}` }, credentials: 'include' })
  if (!response.ok || generation !== sessionVersion.value) throw new Error('Файл недоступен')
  const blob = await response.blob()
  if (generation !== sessionVersion.value) throw new Error('Сессия изменилась')
  const url = URL.createObjectURL(blob), link = document.createElement('a')
  link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url)
}
