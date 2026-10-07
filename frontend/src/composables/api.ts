import { API_BASE, accessToken, sessionVersion, refreshSession, clearSession } from './session'
import { friendlyApiError } from '../components/review/presentation'
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
    throw new Error(friendlyApiError(error, response.status))
  }
  if (response.status === 204) return undefined as T
  const data: T = await response.json()
  if (generation !== sessionVersion.value) throw new Error('Сессия изменилась')
  return data
}
export function post<T>(path: string, data: object): Promise<T> { return api<T>(path, { method: 'POST', body: JSON.stringify(data) }) }
const analyticsErrors: Record<string, string> = { presentation_missing: 'Модель получила данные, но не построила график. Повторите запрос.', ungrounded_financial_response: 'Модель не подтвердила финансовые значения источниками. Повторите запрос.', provider_invalid_response: 'Модель вернула пустой или неверный ответ. Повторите запрос.', analytics_not_configured: 'Доступ к аналитическим данным ещё не настроен администратором.', analytics_query_failed: 'Не удалось прочитать аналитические данные.', unsupported_query: 'Этот разрез пока не поддерживается. Уточните условия.', invalid_tool_arguments: 'Не удалось подобрать корректную выборку. Уточните вопрос.', presentation_invalid: 'Не удалось построить график по этим данным.', provider_tools_unsupported: 'Выбранная модель не поддерживает инструменты аналитики.', dataset_limit_use_filters: 'Слишком много данных. Сузьте период или фильтры.', context_budget_use_filters: 'Выборка слишком велика для модели. Уточните условия.', query_limit_exceeded: 'Превышен лимит аналитического запроса. Уточните вопрос.', analytics_timeout: 'Запрос превысил время обработки. Сузьте выборку.' }
export async function pollOperation<T>(id: number, signal?: AbortSignal): Promise<T> {
  for (let attempt = 0; attempt < 180; attempt++) {
    if (signal?.aborted) throw new Error('Запрос отменён')
    const operation = await api<Operation<T>>(`/operations/${id}/`, { signal })
    if (operation.status === 'succeeded' && operation.result !== null) return operation.result
    if (['failed', 'expired', 'cancelled'].includes(operation.status)) throw new Error(operation.status === 'failed' ? (analyticsErrors[operation.error_code] || 'Не удалось обработать запрос. Попробуйте позже; KPI и кабинет доступны.') : 'Запрос отменён или доступ изменился')
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
