import { ref, watch } from 'vue'
import type { ViewMode, KpiDashboardData, ChatWidget, ChatQuote, ChatResponse } from '../types/chat'
import type { Period, OperationReceipt, KpiFilters } from '../types/platform'
import { validatePresentation, type PresentationDocument } from '../types/presentation'
import { api, post, pollOperation } from './api'
import { currentUser, sessionVersion } from './session'
import { useAuth } from './useAuth'
export function useChat() {
  const currentView = ref<ViewMode>('welcome'), isGenerating = ref(false), activeWidget = ref<ChatWidget | null>(null)
  const activePresentation = ref<PresentationDocument | null>(null), hasChatResponse = ref(false)
  const chatResponseText = ref(''), error = ref(''), quotes = ref<ChatQuote[]>([])
  const kpiData = ref<KpiDashboardData | null>(null), selectedPeriod = ref<Period>('this_month')
  const filters = ref<KpiFilters>({ currency: 'KZT' })
  const welcomeSuggestions = ref(['Покажи график поступлений ↗', 'Покажи KPI команды ↗', 'Какие обещания просрочены? ↗', 'Покажи воронку проектов ↗'])
  const dashboardSuggestions = welcomeSuggestions
  const { isAuthModalOpen } = useAuth()
  let controller: AbortController | undefined
  let operationId: number | undefined
  let generation = 0
  function reset() {
    generation++; controller?.abort(); controller = undefined; isGenerating.value = false
    const pending = operationId; operationId = undefined
    if (pending !== undefined) void api(`/operations/${pending}/`, { method: 'DELETE' }).catch(() => undefined)
    activeWidget.value = null; activePresentation.value = null; chatResponseText.value = ''; quotes.value = []; error.value = ''; hasChatResponse.value = false
  }
  watch(sessionVersion, () => { reset(); kpiData.value = null; currentView.value = 'welcome' })
  async function fetchKpiData(period?: string) {
    if (period && ['this_month', 'last_month', 'quarter', 'year'].includes(period)) { reset(); selectedPeriod.value = period as Period }
    if (!currentUser.value || currentUser.value.roles.includes('client')) return
    error.value = ''
    const ownGeneration = generation
    try { const query = new URLSearchParams({ period: selectedPeriod.value }); for (const [key, value] of Object.entries(filters.value)) { if (value !== undefined) query.set(key, String(value)) }; const data = await api<KpiDashboardData>(`/kpi/summary/?${query}`); if (generation === ownGeneration) kpiData.value = data }
    catch (e) { if (generation === ownGeneration) error.value = e instanceof Error ? e.message : 'Ошибка загрузки KPI' }
  }
  async function handlePromptSubmit(prompt: string) {
    if (!currentUser.value) { isAuthModalOpen.value = true; return }
    if (isGenerating.value) return
    reset()
    const ownGeneration = generation
    const ownController = new AbortController(); controller = ownController
    currentView.value = 'dashboard'; isGenerating.value = true; hasChatResponse.value = true
    try {
      const receipt = await post<OperationReceipt>('/chat/query/', { ...filters.value, prompt, period: selectedPeriod.value, idempotency_key: crypto.randomUUID() })
      if (generation !== ownGeneration) { void api(`/operations/${receipt.operation_id}/`, { method: 'DELETE' }).catch(() => undefined); return }
      operationId = receipt.operation_id
      const response = await pollOperation<ChatResponse>(receipt.operation_id, ownController.signal)
      if (generation !== ownGeneration) return
      let presentation: PresentationDocument | null = null
      if (response.presentation) {
        try { presentation = validatePresentation(response.presentation) }
        catch { throw new Error('Получен неверный формат графиков. Повторите запрос или уточните условия.') }
      }
      activePresentation.value = presentation; chatResponseText.value = response.text; activeWidget.value = presentation ? null : response.widget; quotes.value = response.quotes
      if (response.widget?.type === 'kpi_grid' && !presentation) kpiData.value = response.widget.data
    } catch (e) { if (generation === ownGeneration && !ownController.signal.aborted) error.value = e instanceof Error ? e.message : 'Ошибка обработки' }
    finally { if (generation === ownGeneration) { isGenerating.value = false; operationId = undefined } }
  }
  async function cancel() { reset(); hasChatResponse.value = true; error.value = 'Запрос отменён' }
  async function changeKpiFilters(value: KpiFilters) { reset(); filters.value = value; await fetchKpiData() }
  function goHome() { reset(); currentView.value = 'welcome' }
  return { currentView, isGenerating, activeWidget, activePresentation, hasChatResponse, chatResponseText, quotes, error, welcomeSuggestions, dashboardSuggestions, selectedPeriod, kpiData, fetchKpiData, handlePromptSubmit, goHome, cancel, changeKpiFilters }
}
