import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import type { ViewMode, KpiDashboardData, ChatWidget, ChatQuote, ChatResponse } from '../types/chat'
import type { Period, KpiFilters } from '../types/platform'
import type { AnalyticsTurn, Conversation, ConversationRef, Artifact, QueryPatch, TurnReceipt } from '../types/dialogue'
import { validatePresentation, type PresentationDocument } from '../types/presentation'
import { api, post, pollOperation } from './api'
import { currentUser, sessionVersion } from './session'
import { useAuth } from './useAuth'
import { requestKey } from '../utils/requestKey'
export function useChat() {
  const currentView = ref<ViewMode>('welcome'), activeWidget = ref<ChatWidget | null>(null)
  const activePresentation = ref<PresentationDocument | null>(null), hasChatResponse = ref(false)
  const chatResponseText = ref(''), error = ref(''), quotes = ref<ChatQuote[]>([])
  const kpiData = ref<KpiDashboardData | null>(null), selectedPeriod = ref<Period>('this_month')
  const filters = ref<KpiFilters>({ currency: 'KZT' })
  const turns = ref<AnalyticsTurn[]>([]), conversations = ref<ConversationRef[]>([])
  const conversationId = ref<number | null>(null), selectedTurnId = ref<number | null>(null), selectedArtifact = ref<Artifact | null>(null)
  const posting = ref(0)
  const isGenerating = computed(() => posting.value > 0 || turns.value.some(t => ['queued', 'running'].includes(t.state)))
  const welcomeSuggestions = ref(['Покажи график поступлений ↗', 'Покажи KPI команды ↗', 'Какие обещания просрочены? ↗', 'Покажи воронку проектов ↗'])
  const dashboardSuggestions = welcomeSuggestions
  const { isAuthModalOpen } = useAuth()
  const controllers = new Map<number, AbortController>()
  let scopeChanged = false
  let clearFilters: ('team_id' | 'manager_id' | 'project_id')[] = []
  let refreshRevision = 0
  let generation = 0, selectionRevision = 0, creating: Promise<number> | null = null, timer: ReturnType<typeof setInterval> | undefined
  const storageKey = () => `mazory-conversation-${currentUser.value?.id}`
  function clear() {
    generation++; selectionRevision++; controllers.forEach(c => c.abort()); controllers.clear(); creating = null
    turns.value = []; conversationId.value = null; selectedTurnId.value = null; selectedArtifact.value = null
    activeWidget.value = null; activePresentation.value = null; chatResponseText.value = ''; quotes.value = []; error.value = ''; hasChatResponse.value = false; posting.value = 0
  }
  watch(sessionVersion, () => { clear(); filters.value = { currency: 'KZT' }; selectedPeriod.value = 'this_month'; scopeChanged = false; conversations.value = []; kpiData.value = null; currentView.value = 'welcome' })
  async function fetchKpiData(period?: string) {
    if (period && ['this_month', 'last_month', 'quarter', 'year'].includes(period)) { selectedPeriod.value = period as Period; scopeChanged = true }
    if (!currentUser.value || currentUser.value.roles.includes('client')) return
    const ownGeneration = generation
    try { const query = new URLSearchParams({ period: selectedPeriod.value }); for (const [key, value] of Object.entries(filters.value)) { if (value !== undefined) query.set(key, String(value)) }; const data = await api<KpiDashboardData>(`/kpi/summary/?${query}`); if (generation === ownGeneration) kpiData.value = data }
    catch (e) { if (generation === ownGeneration) error.value = e instanceof Error ? e.message : 'Ошибка загрузки KPI' }
  }
  async function refreshConversation() {
    const id = conversationId.value, ownGeneration = generation, revision = ++refreshRevision
    if (id === null) return
    const conversation = await api<Conversation>(`/chat/conversations/${id}/`)
    if (generation !== ownGeneration || conversationId.value !== id || revision !== refreshRevision) return
    turns.value = conversation.turns
    const summary = conversations.value.find(c => c.id === id)
    if (summary) summary.title = conversation.title
    // Revoked or expired numerical snapshots must disappear from the workspace as well.
    if (selectedTurnId.value !== null) {
      const selected = turns.value.find(t => t.id === selectedTurnId.value)
      const displayed = selectedArtifact.value && turns.value.flatMap(t => t.artifacts).find(a => a.id === selectedArtifact.value?.id)
      if (selected?.state === 'expired' || (selectedArtifact.value && !displayed?.available)) { activePresentation.value = null; selectedArtifact.value = null; chatResponseText.value = '' }
    }
  }
  async function selectTurn(id: number, artifactId?: number, automatically = false) {
    const turn = turns.value.find(t => t.id === id)
    if (!turn && !automatically) return
    selectedTurnId.value = id
    const revision = automatically ? selectionRevision : ++selectionRevision, ownGeneration = generation
    const ref = artifactId === undefined ? turn?.artifacts[0] : automatically ? { id: artifactId } : turn?.artifacts.find(a => a.id === artifactId)
    if (!ref) return
    try {
      const artifact = await api<Artifact>(`/chat/artifacts/${ref.id}/`)
      if (generation !== ownGeneration || revision !== selectionRevision) return
      selectedArtifact.value = artifact
      activePresentation.value = artifact.available && artifact.presentation ? validatePresentation(artifact.presentation) : null
    } catch (e) { if (generation === ownGeneration && revision === selectionRevision) error.value = e instanceof Error ? e.message : 'Результат недоступен' }
  }
  async function loadConversation(id: number) {
    clear(); const ownGeneration = generation; conversationId.value = id
    try {
      await refreshConversation()
      if (generation !== ownGeneration) return
      hasChatResponse.value = true; currentView.value = 'dashboard'; localStorage.setItem(storageKey(), JSON.stringify(id))
      const completed = [...turns.value].reverse().find(t => t.state === 'succeeded' && t.artifacts.length)
      if (completed) await selectTurn(completed.id)
    } catch (e) { if (generation === ownGeneration) { conversationId.value = null; error.value = e instanceof Error ? e.message : 'Диалог недоступен' } }
  }
  async function restoreConversations() {
    if (!currentUser.value || currentUser.value.roles.includes('client')) return
    const ownGeneration = generation
    try {
      const list = await api<ConversationRef[]>('/chat/conversations/')
      if (generation !== ownGeneration) return
      conversations.value = list
      const stored: unknown = JSON.parse(localStorage.getItem(storageKey()) || 'null')
      if (conversationId.value === null && !creating && posting.value === 0 && typeof stored === 'number' && list.some(c => c.id === stored)) await loadConversation(stored)
    } catch { /* New conversations remain available if no previous history exists. */ }
  }
  async function ensureConversation() {
    if (conversationId.value !== null) return conversationId.value
    if (!creating) {
      const ownGeneration = generation
      creating = post<ConversationRef>('/chat/conversations/', { ...filters.value, period: selectedPeriod.value }).then(c => {
        if (generation !== ownGeneration) throw new Error('Диалог изменился')
        conversationId.value = c.id; localStorage.setItem(storageKey(), JSON.stringify(c.id)); conversations.value.unshift(c); return c.id
      }).finally(() => { if (generation === ownGeneration) creating = null })
    }
    return creating
  }
  async function handlePromptSubmit(prompt: string, patch?: QueryPatch) {
    if (!currentUser.value) { isAuthModalOpen.value = true; return }
    if (!prompt.trim()) return
    const ownGeneration = generation, selectionAtSubmit = ++selectionRevision
    currentView.value = 'dashboard'; hasChatResponse.value = true; error.value = ''; posting.value++
    let receipt: TurnReceipt | undefined
    const controller = new AbortController()
    try {
      const id = await ensureConversation()
      if (generation !== ownGeneration) return
      const selected = turns.value.find(t => t.id === selectedTurnId.value)
      const latest = turns.value.at(-1)
      const parent = selected || latest
      receipt = await post<TurnReceipt>(`/chat/conversations/${id}/turns/`, { ...(!parent || scopeChanged ? { ...filters.value, period: selectedPeriod.value } : {}), prompt, idempotency_key: requestKey(),
        ...(parent ? { parent_turn_id: parent.id } : {}), ...(selectedArtifact.value?.id && selected?.artifacts.some(a => a.id === selectedArtifact.value?.id) ? { artifact_id: selectedArtifact.value.id } : {}), ...(patch ? { patch } : {}), ...(scopeChanged && clearFilters.length ? { clear_filters: clearFilters } : {}) })
      if (generation !== ownGeneration) { void api(`/operations/${receipt.operation_id}/`, { method: 'DELETE' }).catch(() => undefined); return }
      scopeChanged = false; clearFilters = []
      controllers.set(receipt.operation_id, controller)
      await refreshConversation()
      // Subsequent messages wait for this exact turn even before its artifact exists.
      if (selectionRevision === selectionAtSubmit) selectedTurnId.value = receipt.turn_id
      const response = await pollOperation<ChatResponse>(receipt.operation_id, controller.signal)
      if (generation !== ownGeneration || controller.signal.aborted) return
      chatResponseText.value = response.text; quotes.value = response.quotes || []
      await refreshConversation()
      if (selectedTurnId.value === receipt.turn_id && selectionRevision === selectionAtSubmit) await selectTurn(receipt.turn_id, response.artifact_id, true)
    } catch (e) {
      if (generation === ownGeneration && !controller.signal.aborted) { error.value = e instanceof Error ? e.message : 'Ошибка обработки'; await refreshConversation().catch(() => undefined) }
    } finally {
      if (receipt) controllers.delete(receipt.operation_id)
      if (generation === ownGeneration) posting.value = Math.max(0, posting.value - 1)
    }
  }
  async function cancel(id?: number) {
    const targets = id === undefined ? turns.value.filter(t => ['queued', 'running'].includes(t.state)).map(t => t.operation_id).filter((v): v is number => v !== null) : [id]
    for (const target of targets) { controllers.get(target)?.abort(); await api(`/operations/${target}/`, { method: 'DELETE' }) }
    await refreshConversation()
  }
  async function followup(turnId: number, artifactId: number | undefined, prompt: string, patch?: QueryPatch) {
    const ownGeneration = generation
    await selectTurn(turnId, artifactId)
    if (generation === ownGeneration && selectedTurnId.value === turnId) await handlePromptSubmit(prompt, patch)
  }
  async function changeKpiFilters(value: KpiFilters) { clearFilters = (['team_id', 'manager_id', 'project_id'] as const).filter(key => value[key] === undefined); filters.value = value; scopeChanged = true; await fetchKpiData() }
  function newConversation() { clear(); localStorage.removeItem(storageKey()); hasChatResponse.value = true; currentView.value = 'dashboard' }
  function goHome() { clear(); localStorage.removeItem(storageKey()); currentView.value = 'welcome' }
  onMounted(() => { timer = setInterval(() => { if (conversationId.value !== null) void refreshConversation().catch(() => undefined) }, 2500) })
  onBeforeUnmount(() => { if (timer) clearInterval(timer); controllers.forEach(c => c.abort()) })
  return { currentView, isGenerating, activeWidget, activePresentation, hasChatResponse, chatResponseText, quotes, error, welcomeSuggestions, dashboardSuggestions, selectedPeriod, kpiData, fetchKpiData, handlePromptSubmit, goHome, cancel, changeKpiFilters,
    turns, conversations, conversationId, selectedTurnId, selectedArtifact, selectTurn, followup, loadConversation, newConversation, restoreConversations }
}
