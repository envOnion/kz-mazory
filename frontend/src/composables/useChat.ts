import { ref, watch } from 'vue'
import type { ViewMode, KpiDashboardData, ChatWidget, ChatQuote, ChatResponse } from '../types/chat'
import type { Period, OperationReceipt, KpiFilters } from '../types/platform'
import { api, post, pollOperation } from './api'
import { currentUser, sessionVersion } from './session'
import { useAuth } from './useAuth'
export function useChat() {
  const currentView = ref<ViewMode>('welcome'), isGenerating = ref(false), activeWidget = ref<ChatWidget | null>(null)
  const chatResponseText = ref(''), error = ref(''), quotes = ref<ChatQuote[]>([])
  const kpiData = ref<KpiDashboardData | null>(null), selectedPeriod = ref<Period>('this_month')
  const filters = ref<KpiFilters>({ currency: 'KZT' })
  const welcomeSuggestions = ref(['Покажи график поступлений ↗', 'Покажи KPI команды ↗', 'Какие обещания просрочены? ↗', 'Покажи воронку проектов ↗'])
  const dashboardSuggestions = welcomeSuggestions
  const { isAuthModalOpen } = useAuth()
  let controller: AbortController | undefined
  let operationId: number | undefined
  watch(sessionVersion, () => { controller?.abort(); kpiData.value = null; activeWidget.value = null; quotes.value = []; chatResponseText.value = ''; error.value = ''; currentView.value = 'welcome' })
  async function fetchKpiData(period?: string) {
    if (period && ['this_month', 'last_month', 'quarter', 'year'].includes(period)) { selectedPeriod.value = period as Period; activeWidget.value = null; chatResponseText.value = ''; quotes.value = [] }
    if (!currentUser.value || currentUser.value.roles.includes('client')) return
    error.value = ''
    try { const query = new URLSearchParams({ period: selectedPeriod.value }); for (const [key, value] of Object.entries(filters.value)) { if (value !== undefined) query.set(key, String(value)) }; kpiData.value = await api<KpiDashboardData>(`/kpi/summary/?${query}`) }
    catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка загрузки KPI' }
  }
  async function handlePromptSubmit(prompt: string) {
    if (!currentUser.value) { isAuthModalOpen.value = true; return }
    if (isGenerating.value) return
    currentView.value = 'dashboard'; isGenerating.value = true; error.value = ''; activeWidget.value = null; chatResponseText.value = ''; quotes.value = []
    controller = new AbortController()
    try {
      const receipt = await post<OperationReceipt>('/chat/query/', { ...filters.value, prompt, period: selectedPeriod.value, idempotency_key: crypto.randomUUID() })
      operationId = receipt.operation_id
      const response = await pollOperation<ChatResponse>(receipt.operation_id, controller.signal)
      chatResponseText.value = response.text; activeWidget.value = response.widget; quotes.value = response.quotes
      if (response.widget?.type === 'kpi_grid') kpiData.value = response.widget.data
    } catch (e) { if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : 'Ошибка обработки' }
    finally { isGenerating.value = false; operationId = undefined }
  }
  async function cancel() {
    controller?.abort(); isGenerating.value = false
    if (operationId !== undefined) await api(`/operations/${operationId}/`, { method: 'DELETE' }).catch(() => undefined)
  }
  async function changeKpiFilters(value: KpiFilters) { filters.value = value; activeWidget.value = null; chatResponseText.value = ''; quotes.value = []; await fetchKpiData() }
  function goHome() { currentView.value = 'welcome'; activeWidget.value = null }
  return { currentView, isGenerating, activeWidget, chatResponseText, quotes, error, welcomeSuggestions, dashboardSuggestions, selectedPeriod, kpiData, fetchKpiData, handlePromptSubmit, goHome, cancel, changeKpiFilters }
}
