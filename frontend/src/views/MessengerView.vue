<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, RefreshCw, ChevronDown, PanelRightOpen, PanelLeftOpen, X } from 'lucide-vue-next'
import TraceContextPanel from '../shell/TraceContextPanel.vue'
import FullHistoryProgress from '../shell/FullHistoryProgress.vue'
import EntityStageProgress from '../shell/EntityStageProgress.vue'
import { routeId } from '../utils/routeId'
import { requestKey } from '../utils/requestKey'
import { api, post } from '../composables/api'
import { accessToken } from '../composables/session'
import { EmptyState, ErrorState, Skeleton, StatusBadge, statusLabel, formatTime } from '../shell/bits'
import type { ChainNode, ChangeRow, RecordChain, DialogSummary, Paged, RecordBrief, RecordDetail, TraceRow, ErrorRecoveryPreview, ErrorRecoveryProgress } from '../types/messenger'

function entityReconciliations(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

// Чаты: список диалогов, лента сообщений, инспектор (Решение / История / Диагностика ИИ).
// Состояние (диалог, сообщение, вкладка, категория) — в URL; обновление — cursor polling 5 c.

const route = useRoute()
const router = useRouter()

const CATEGORY_LABELS: Record<string, string> = { all: 'Все', group: 'Группы', private: 'Личные', system: 'Система' }
const PURPOSE_LABELS: Record<string, string> = {
  source: 'Сообщение', clarification: 'Уточнение системы', reply: 'Ответ на уточнение',
  notification: 'Уведомление', auth: 'Код входа', system: 'Служебное',
}
const KIND_LABELS: Record<string, string> = {
  received:'Сообщение получено',analysis_started:'Начат анализ',analysis_finished:'Анализ завершён',analysis_error:'Ошибка анализа',
  clarification_created:'Уточнение создано',clarification_state:'Состояние уточнения',answer_interpreted:'Ответ обработан',crm_state:'Состояние доставки в CRM',
  history_reconstructed:'Восстановлено из истории',unknown_result:'Результат неизвестен',reanalyze:'Повторный анализ',delivery_failed:'Ошибка доставки',sent:'Отправлено провайдеру',ack:'Подтверждение провайдера',
  inbound: 'Входящее', outbound: 'Исходящее', internal: 'Служебное',
  analysis: 'Анализ', clarification: 'Уточнение', reply: 'Ответ', delivery: 'Попытка доставки',
  dispatch: 'Отправка', crm_delivery: 'Изменение CRM', otp: 'Код входа', notification: 'Уведомление',
  message_received: 'Получено сообщение', record: 'Запись', event: 'Событие',
}

// ---- размеры / панели ----
const wide = ref(window.innerWidth >= 1130)
const roomy = ref(window.innerWidth >= 761)
const inspectorOpen = ref(window.innerWidth >= 1130)
const listOpen = ref(true)
function onResize() {
  const wasWide = wide.value
  wide.value = window.innerWidth >= 1130
  roomy.value = window.innerWidth >= 761
  if (wide.value) inspectorOpen.value = true
  else if (wasWide) inspectorOpen.value = false
  if (roomy.value) listOpen.value = true
  else if (selected.value) listOpen.value = false
}
let inspectorTrigger: HTMLElement | null = null
let listTrigger: HTMLElement | null = null
watch(inspectorOpen, async open => {
  if (wide.value) return
  if (open) { inspectorTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null; await nextTick(); document.querySelector<HTMLElement>('[data-inspector] button')?.focus() }
  else inspectorTrigger?.focus()
})
watch(listOpen, async open => {
  if (roomy.value) return
  if (open) { listTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null; await nextTick(); document.querySelector<HTMLElement>('[data-dialog-list] button')?.focus() }
  else listTrigger?.focus()
})
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Tab') {
    const panel = !wide.value && inspectorOpen.value ? '[data-inspector]' : !roomy.value && listOpen.value ? '[data-dialog-list]' : null
    if (panel) {
      const elements = Array.from(document.querySelectorAll<HTMLElement>(`${panel} button, ${panel} input, ${panel} a, ${panel} [tabindex="0"]`)).filter(el => el.getClientRects().length && !el.hasAttribute('disabled'))
      const first = elements[0], last = elements.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    return
  }
  if (event.key !== 'Escape') return
  if (!wide.value && inspectorOpen.value) { inspectorOpen.value = false; return }
  if (!roomy.value && listOpen.value) listOpen.value = false
}

// ---- список диалогов ----
const dialogs = ref<DialogSummary[]>([])
const dialogsError = ref('')
const dialogsLoading = ref(true)
const dialogsNext = ref<string|null>(null)
let dialogsPages = 1
const searchInput = ref(String(route.query.q || ''))
const attentionOnly = ref(route.query.attention === '1')
let searchTimer: number | null = null
let dialogsRequest: AbortController | null = null
let feedRequest: AbortController | null = null
let detailRequest: AbortController | null = null
let tabRequest: AbortController | null = null
let stopped = false
const detailError = ref('')

const category = computed(() => {
  const value = String(route.query.cat2 || 'all')
  return value in CATEGORY_LABELS ? value : 'all'
})
function cohortParams(params:URLSearchParams) {
  for(const key of ['analysis','cohort'] as const) if(typeof route.query[key]==='string') params.set(key,route.query[key])
  if(attentionOnly.value) params.set('attention','1')
}
function matchesCohort(record:RecordBrief) {
  return (!route.query.analysis || record.statuses.analysis===route.query.analysis) &&
    (!attentionOnly.value || record.attention) &&
    (route.query.cohort !== 'source' || (record.direction==='inbound' && record.purpose==='source'))
}

async function loadDialogs(showLoading = true, append = false) {
  if (!showLoading && dialogsRequest) return
  dialogsRequest?.abort()
  const request = new AbortController(); dialogsRequest = request
  let timedOut = false
  const timeout = window.setTimeout(() => { timedOut = true; request.abort() }, 20000)
  if (showLoading) dialogsLoading.value = true
  dialogsError.value = ''
  if (showLoading && !append) dialogsPages = 1
  try {
    const params = new URLSearchParams()
    cohortParams(params)
    if (category.value !== 'all') params.set('category', category.value)
    const q = searchInput.value.trim()
    if (q) params.set('q', q)
    if (attentionOnly.value) params.set('attention', '1')
    let cursor = append ? dialogsNext.value : null
    const rows = new Map((append ? dialogs.value : []).map(row=>[row.key,row]))
    const pages = append ? 1 : dialogsPages
    for(let index=0;index<pages;index++) {
      if(cursor) params.set('cursor',cursor)
      else params.delete('cursor')
      const page = await api<Paged<DialogSummary>>(`/messenger/dialogs/?${params}`, {signal:request.signal})
      if(request.signal.aborted) return
      for(const row of page.items) rows.set(row.key,row)
      cursor=page.next_cursor
      if(!cursor) break
    }
    if (!request.signal.aborted) { dialogs.value = [...rows.values()]; dialogsNext.value = cursor; if(append) dialogsPages++ }
  } catch (error) {
    if (dialogsRequest === request) {
      if (timedOut) dialogsError.value = 'Не удалось загрузить диалоги за 20 секунд. Повторите запрос.'
      else if (!request.signal.aborted) dialogsError.value = error instanceof Error ? error.message : 'Ошибка сети'
    }
  } finally {
    clearTimeout(timeout)
    if (dialogsRequest === request) { dialogsRequest = null; dialogsLoading.value = false }
  }
}

// ---- выбранный диалог ----
interface DialogSel { s: string; c: string; cat: string; t: string }
const selected = computed<DialogSel | null>(() => {
  const { s, c, cat } = route.query
  if (s === undefined || c === undefined || !cat) return null
  return { s: String(s), c: String(c), cat: String(cat), t: String(route.query.t ?? '') }
})
const selKey = computed(() => selected.value ? `${selected.value.s}|${selected.value.c}|${selected.value.cat}|${selected.value.t}` : '')

const recoveryPreview = ref<ErrorRecoveryPreview | null>(null)
const recoveryProgress = ref<ErrorRecoveryProgress | null>(null)
const recoveryError = ref(''), recoveryBusy = ref(false), recoveryOpen = ref(false)
let recoveryTimer: number | undefined
let recoveryRequestKey = requestKey()
function recoveryParams() {
  const params = new URLSearchParams()
  const dialog = selected.value
  if (dialog) {
    params.set('session_name', dialog.s); params.set('chat_id', dialog.c); params.set('category', dialog.cat)
    if (dialog.t) params.set('team', dialog.t)
  }
  return params
}
async function refreshRecovery(operationId: number) {
  const selection = selKey.value
  const params = recoveryParams(); params.set('operation_id', String(operationId))
  try {
    const progress = await api<ErrorRecoveryProgress>(`/messenger/reanalyze-errors/?${params}`)
    if (selection !== selKey.value || stopped) return
    recoveryProgress.value = progress; recoveryOpen.value = true
    if (progress.status === 'queued' || progress.status === 'running' || progress.pending > 0) {
      recoveryTimer = window.setTimeout(() => { void refreshRecovery(operationId) }, 5000)
    }
  } catch (error) { if (selection === selKey.value) recoveryError.value = error instanceof Error ? error.message : 'Не удалось обновить повторный анализ' }
}
async function openRecovery() {
  recoveryOpen.value = !recoveryOpen.value
  if (!recoveryOpen.value || recoveryProgress.value) return
  const selection = selKey.value
  recoveryBusy.value = true; recoveryError.value = ''
  try {
    const preview = await api<ErrorRecoveryPreview>(`/messenger/reanalyze-errors/?${recoveryParams()}`)
    if (selection === selKey.value) { recoveryPreview.value = preview; recoveryRequestKey = requestKey() }
  } catch (error) { if (selection === selKey.value) recoveryError.value = error instanceof Error ? error.message : 'Не удалось проверить ошибки' }
  finally { if (selection === selKey.value) recoveryBusy.value = false }
}
async function startRecovery() {
  const selection = selKey.value
  recoveryBusy.value = true; recoveryError.value = ''
  try {
    const progress = await post<ErrorRecoveryProgress>(`/messenger/reanalyze-errors/?${recoveryParams()}`, { idempotency_key: recoveryRequestKey })
    if (selection !== selKey.value) return
    recoveryProgress.value = progress
    await router.replace({ query: { ...route.query, recovery: String(progress.operation_id) } })
  } catch (error) { if (selection === selKey.value) recoveryError.value = error instanceof Error ? error.message : 'Не удалось поставить повторный анализ' }
  finally { if (selection === selKey.value) recoveryBusy.value = false }
}
watch(selKey, () => {
  if (recoveryTimer) clearTimeout(recoveryTimer)
  recoveryPreview.value = null; recoveryProgress.value = null; recoveryError.value = ''; recoveryBusy.value = false; recoveryOpen.value = false
})
watch(() => route.query.recovery, value => {
  if (recoveryTimer) clearTimeout(recoveryTimer)
  const id = routeId(value)
  if (id) void refreshRecovery(id)
}, { immediate: true })
onBeforeUnmount(() => { if (recoveryTimer) clearTimeout(recoveryTimer) })

function openDialog(dialog: DialogSummary) {
  listOpen.value = roomy.value
  void router.push({ path: '/messenger', query: {
    ...route.query, recovery:undefined, record:searchInput.value.trim() && dialog.match_record_id ? String(dialog.match_record_id) : undefined, tab:undefined,
    s: dialog.session_name, c: dialog.chat_id, cat: dialog.category,
    t: dialog.team_id === null ? '' : String(dialog.team_id),
    ...(searchInput.value.trim() ? { q: searchInput.value.trim() } : {}),
    ...(attentionOnly.value ? { attention: '1' } : {}),
    ...(category.value !== 'all' ? { cat2: category.value } : {}),
  } })
}

function patchQuery(values: Record<string, string | null>) {
  const query = { ...route.query } as Record<string, string>
  for (const [key, value] of Object.entries(values)) {
    if (value === null) delete query[key]
    else query[key] = value
  }
  void router.push({ path: '/messenger', query })
}

// ---- лента ----
const feed = ref<RecordBrief[]>([])
const feedError = ref('')
const feedLoading = ref(false)
const olderCursor = ref<string | null>(null)
const loadingOlder = ref(false)
const detail = ref<RecordDetail | null>(null)
const selectedId = computed(() => routeId(route.query.record))
const tab = computed(() => route.query.tab === 'history' || route.query.tab === 'trace' ? route.query.tab : 'decision')

const feedBox = ref<HTMLElement | null>(null)
const atBottom = ref(true)
const newCount = ref(0)
const unseen = new Set<number>()

async function loadFeed(around: number | null) {
  feedRequest?.abort()
  const request = new AbortController(); feedRequest = request
  if (!selected.value) { feed.value = []; return }
  feedLoading.value = true
  feedError.value = ''
  try {
    const params = new URLSearchParams()
    params.set('session_name', selected.value.s)
    cohortParams(params)
    params.set('chat_id', selected.value.c)
    params.set('category', selected.value.cat)
    if (selected.value.t) params.set('team', selected.value.t)
    if (around) params.set('around', String(around))
    const page = await api<Paged<RecordBrief>>(`/messenger/records/?${params}`, {signal:request.signal})
    if (request.signal.aborted) return
    feed.value = [...page.items].reverse()
    olderCursor.value = page.next_cursor
    await nextTick()
    if (around) document.querySelector(`[data-record-id="${around}"]`)?.scrollIntoView({block:'center'})
    else scrollToBottom()
  } catch (error) {
    if (!request.signal.aborted) feedError.value = error instanceof Error ? error.message : 'Ошибка сети'
  } finally { if (!request.signal.aborted) feedLoading.value = false }
}

async function loadOlder() {
  if (!olderCursor.value || !selected.value || loadingOlder.value) return
  loadingOlder.value = true
  const key = selKey.value
  try {
    const params = new URLSearchParams()
    params.set('session_name', selected.value.s)
    params.set('chat_id', selected.value.c)
    params.set('category', selected.value.cat)
    if (selected.value.t) params.set('team', selected.value.t)
    params.set('cursor', olderCursor.value)
    cohortParams(params)
    const page = await api<Paged<RecordBrief>>(`/messenger/records/?${params}`)
    if (key !== selKey.value) return
    const box = feedBox.value
    const prevHeight = box ? box.scrollHeight - box.scrollTop : 0
    const known = new Set(feed.value.map(r => r.id))
    feed.value = [...[...page.items].reverse().filter(r => !known.has(r.id)), ...feed.value]
    olderCursor.value = page.next_cursor
    await nextTick()
    if (box) box.scrollTop = box.scrollHeight - prevHeight
  } catch (error) { if (key === selKey.value) feedError.value = error instanceof Error ? error.message : 'Не удалось загрузить историю' }
  finally { loadingOlder.value = false }
}

function scrollToBottom() {
  const box = feedBox.value
  if (box) box.scrollTop = box.scrollHeight
  newCount.value = 0
  unseen.clear()
  atBottom.value = true
}
function onFeedScroll() {
  const box = feedBox.value
  if (!box) return
  atBottom.value = box.scrollHeight - box.scrollTop - box.clientHeight < 80
  if (box.scrollTop < 140 && olderCursor.value) void loadOlder()
  if (atBottom.value && newCount.value) newCount.value = 0
}

// ---- инспектор ----
const chain = ref<RecordChain | null>(null)
const chainError = ref('')
const traces = ref<TraceRow[] | null>(null)
const tracesError = ref('')
const tracesCursor = ref<string|null>(null)
const reanalyzeState = ref<'idle' | 'confirm' | 'sent'>('idle')

async function loadDetail() {
  detailRequest?.abort(); tabRequest?.abort()
  const request = new AbortController(); detailRequest = request
  if (detail.value?.id !== selectedId.value) { chain.value = null; traces.value = null; tracesCursor.value = null }
  tracesError.value = ''; detailError.value = ''
  reanalyzeState.value = 'idle'
  if (!selectedId.value) { detail.value = null; return }
  try {
    const result = await api<RecordDetail>(`/messenger/records/${selectedId.value}/`, {signal:request.signal})
    if (request.signal.aborted) return
    detail.value = result
    if (!selected.value) {
      await router.replace({path:'/messenger',query:{...route.query,s:result.dialog.session_name,c:result.dialog.chat_id,cat:result.dialog.category,t:result.dialog.team_id === null ? '' : String(result.dialog.team_id)}})
    }
    void loadTab()
  } catch (error) {
    if (!request.signal.aborted) { detail.value = null; detailError.value = error instanceof Error ? error.message : 'Сообщение недоступно' }
  }
}
async function loadTab() {
  tabRequest?.abort()
  if (!detail.value) return
  const request = new AbortController(); tabRequest = request
  if (tab.value === 'decision' || tab.value === 'history') {
    chainError.value = ''
    try {
      const page = await api<RecordChain>(`/messenger/records/${detail.value.id}/chain/`, {signal:request.signal})
      if (!request.signal.aborted) chain.value = page
    } catch (error) { if (!request.signal.aborted) chainError.value = error instanceof Error ? error.message : 'Ошибка сети' }
  }
  if (tab.value === 'trace') {
    if (!detail.value.capabilities.can_trace) { traces.value = null; return }
    tracesError.value = ''
    try {
      const page = await api<Paged<TraceRow>>(`/messenger/records/${detail.value.id}/traces/`, {signal:request.signal})
      if (!request.signal.aborted) {
        const existing = new Map((traces.value ?? []).map(row => [row.id,row])); for (const row of page.items) existing.set(row.id,row)
        if (!traces.value?.length) tracesCursor.value = page.next_cursor
        traces.value = [...existing.values()].sort((a,b) => b.id-a.id)
      }
    } catch (error) { if (!request.signal.aborted) { traces.value = null; tracesError.value = error instanceof Error ? error.message : 'Ошибка сети' } }
  }
}
async function moreTraces() {
  if (!detail.value || !tracesCursor.value) return
  const id = detail.value.id
  try { const page = await api<Paged<TraceRow>>(`/messenger/records/${id}/traces/?cursor=${encodeURIComponent(tracesCursor.value)}`); if (detail.value?.id !== id) return; const existing = new Map((traces.value ?? []).map(row => [row.id,row])); for (const row of page.items) existing.set(row.id,row); traces.value = [...existing.values()].sort((a,b)=>b.id-a.id); tracesCursor.value = page.next_cursor }
  catch(e) { tracesError.value = e instanceof Error ? e.message : 'Не удалось загрузить попытки' }
}
async function moreEvents() {
  if (!detail.value || !chain.value?.next_cursor) return
  const id=detail.value.id
  try { const result=await api<RecordChain>(`/messenger/records/${id}/chain/?cursor=${encodeURIComponent(chain.value.next_cursor)}`); if(detail.value?.id !== id || !chain.value) return; const rows=new Map(chain.value.timeline.map(row=>[row.id,row])); for(const row of result.timeline) rows.set(row.id,row); chain.value.timeline=[...rows.values()]; chain.value.next_cursor=result.next_cursor }
  catch(e) { chainError.value=e instanceof Error ? e.message : 'Не удалось загрузить историю' }
}
watch(tab, loadTab)

function gotoNode(node: ChainNode) {
  if (node.allowed && node.href) void router.push(node.href)
}

async function reanalyze() {
  if (!detail.value) return
  if (reanalyzeState.value !== 'confirm') { reanalyzeState.value = 'confirm'; return }
  try {
    await post<{ operation_id: number }>(`/messenger/records/${detail.value.id}/reanalyze/`, {
      idempotency_key: requestKey(),
    })
    reanalyzeState.value = 'sent'
    startChanges(true)
  } catch (error) {
    reanalyzeState.value = 'idle'
    tracesError.value = error instanceof Error ? error.message : 'Не удалось поставить повтор'
  }
}

function selectRecord(id: number) {
  inspectorOpen.value = wide.value
  patchQuery({ record: String(id) })
}

// ---- обновление: cursor polling 5 c, пауза на скрытой вкладке, backoff до 30 c ----
let changesCursor: string | null = null
let pollTimer: number | null = null
let pollDelay = 5000
let pollBusy = false
const stale = ref(false)

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  if (document.hidden || stopped) return
  pollTimer = window.setTimeout(() => void pollChanges(), pollDelay)
}

async function pollChanges(immediate = false) {
  if (pollBusy || stopped) return
  if (!accessToken.value) { schedulePoll(); return }
  if (!immediate && document.hidden) { schedulePoll(); return }
  pollBusy = true
  try {
    const url = changesCursor ? `/messenger/changes/?cursor=${enco(changesCursor)}` : '/messenger/changes/'
    const page = await api<{ items: ChangeRow[]; next_cursor: string | null }>(url)
    changesCursor = page.next_cursor
    pollDelay = 5000
    stale.value = false
    if (page.items.length) await applyChanges(page.items)
    void loadDialogs(false)
  } catch {
    pollDelay = Math.min(pollDelay * 2, 30000)
    stale.value = true
  }
  pollBusy = false
  schedulePoll()
}
function enco(value: string) { return encodeURIComponent(value) }


async function applyChanges(rows: ChangeRow[]) {
  const key = selKey.value
  const known = new Map(feed.value.map(r => [r.id, r]))
  const targets = new Set<number>()
  let added = 0
  for (const row of rows) {
    if (row.type === 'record' && row.dialog && selected.value &&
        row.dialog.session_name === selected.value.s && row.dialog.chat_id === selected.value.c &&
        row.dialog.category === selected.value.cat &&
        (row.dialog.team_id === null ? '' : String(row.dialog.team_id)) === selected.value.t) {
      if(!matchesCohort(row)){known.delete(row.id);continue}
      if (!known.has(row.id)) { added++; unseen.add(row.id) }
      known.set(row.id, row)
    } else if (row.type === 'event' && row.record_id) targets.add(row.record_id)
  }
  for (const id of targets) if (known.has(id)) {
    const result = await api<RecordDetail>(`/messenger/records/${id}/`)
    if (key !== selKey.value || stopped) return
    if(matchesCohort(result)) known.set(id, result)
    else known.delete(id)
  }
  if (key !== selKey.value || stopped) return
  feed.value = [...known.values()].sort((a,b) => (a.occurred_at ?? '').localeCompare(b.occurred_at ?? '') || a.id-b.id)
  if (targets.has(selectedId.value ?? -1)) void loadDetail()
  if (added && atBottom.value) await nextTick(scrollToBottom)
  else if (added) newCount.value = unseen.size
}

function onVisibility() {
  if (!document.hidden) { startChanges(true) }
  else if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
}
function startChanges(immediate = false) { void pollChanges(immediate) }

// ---- загрузка и слежение за URL ----
watch(selKey, () => { unseen.clear(); newCount.value = 0; feed.value = []; void loadFeed(selectedId.value) })
watch(selectedId, id => { void loadDetail(); if(id && selected.value && !feed.value.some(row=>row.id===id)) void loadFeed(id) })
watch(() => route.query.attention, value => { attentionOnly.value = value === '1' })
watch(() => route.query.q, value => { searchInput.value = typeof value === 'string' ? value : '' })
watch(attentionOnly, value => patchQuery({attention:value ? '1' : null}))
watch(searchInput, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => { patchQuery({q:searchInput.value.trim() || null}); void loadDialogs() }, 300)
})
watch([category, attentionOnly, () => route.query.analysis, () => route.query.cohort], () => void loadDialogs())
watch([() => route.query.analysis,() => route.query.cohort,() => route.query.attention], () => void loadFeed(selectedId.value))

function setCategory(value: string) {
  patchQuery({ cat2: value === 'all' ? null : value })
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  window.addEventListener('keydown', onKeydown)
  document.addEventListener('visibilitychange', onVisibility)
  onResize()
  attentionOnly.value = route.query.attention === '1'
  void loadDialogs()
  void loadFeed(selectedId.value)
  if (selectedId.value) void loadDetail()
  startChanges(true)
})
onBeforeUnmount(() => {
  stopped = true; dialogsRequest?.abort(); feedRequest?.abort(); detailRequest?.abort(); tabRequest?.abort()
  if (searchTimer) clearTimeout(searchTimer)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('keydown', onKeydown)
  document.removeEventListener('visibilitychange', onVisibility)
  if (pollTimer) clearTimeout(pollTimer)
})

const dialogTitle = computed(() => dialogs.value.find(d => d.key === selKey.value)?.name ||
  (selected.value ? (selected.value.s || selected.value.c || 'Диалог') : ''))
const dialogCategory = computed(() => selected.value ? CATEGORY_LABELS[selected.value.cat] || selected.value.cat : '')

function bubbleClass(record: RecordBrief) {
  if (record.category === 'system' || record.direction === 'internal') return 'system'
  return record.direction === 'outbound' ? 'out' : 'in'
}
const timelineOrdered = computed(() => chain.value?.timeline || [])
</script>

<template>
  <div v-if="!roomy && listOpen" class="mx-overlay" @click="listOpen = false" />
  <!-- Список диалогов -->
  <aside class="mx-panel-list flex flex-col" :class="{ 'mx-list-hidden': !roomy && !listOpen }" data-dialog-list :role="!roomy ? 'dialog' : undefined" :aria-modal="!roomy && listOpen ? true : undefined" aria-label="Диалоги">
    <div class="p-3 pb-2" style="border-bottom:1px solid var(--mx-border)">
      <div class="flex justify-between"><h1 style="font-size:23px;font-weight:600" class="leading-tight">Чаты</h1><button v-if="!roomy" class="mx-btn" aria-label="Закрыть список диалогов" @click="listOpen=false"><X :size="16" /></button></div>
      <div class="flex gap-1 mt-2 flex-wrap" role="tablist" aria-label="Категории">
        <button
          v-for="(label, value) in CATEGORY_LABELS" :key="value" role="tab"
          class="mx-chip" :class="category === value ? 'mx-chip-info' : 'mx-chip-muted'"
          :aria-selected="category === value" @click="setCategory(value)"
        >{{ label }}</button>
        <button
          class="mx-chip" :class="attentionOnly ? 'mx-chip-warn' : 'mx-chip-muted'"
          :aria-pressed="attentionOnly" @click="attentionOnly = !attentionOnly"
        >Требует внимания</button>
      </div>
      <div class="relative mt-2">
        <Search :size="14" class="absolute left-3 top-1/2 -translate-y-1/2" style="color:var(--mx-muted)" />
        <input v-model="searchInput" class="mx-input" style="padding-left:32px" placeholder="Поиск по чатам и сообщениям" aria-label="Поиск по чатам" />
      </div>
      <p v-if="route.query.analysis || route.query.cohort" class="mx-muted mt-2 text-xs">{{ route.query.analysis ? statusLabel(String(route.query.analysis)) : 'Исходные входящие сообщения' }} <button class="mx-link" @click="patchQuery({analysis:null,cohort:null})">Сбросить выборку</button></p>
    </div>
    <div class="flex-1 overflow-y-auto">
      <Skeleton v-if="dialogsLoading && !dialogs.length" :rows="6" height="44px" />
      <ErrorState v-else-if="dialogsError" :text="dialogsError" :on-retry="loadDialogs" />
      <EmptyState v-else-if="!dialogs.length" title="Диалогов нет" text="По текущим фильтрам и правам сообщений не найдено." />
      <button
        v-for="dialog in dialogs" :key="dialog.key"
        class="w-full text-left px-3 py-2.5 flex gap-2.5 items-start hover:bg-[rgba(255,255,255,0.03)]"
        :style="selKey === dialog.key ? 'background:rgba(152,126,255,0.10)' : undefined"
        :aria-current="selKey === dialog.key ? 'true' : undefined"
        @click="openDialog(dialog)"
      >
        <span class="mx-chip mx-chip-muted shrink-0" style="min-height:28px;padding:0 8px" :title="dialog.category === 'group' ? 'Группа' : dialog.category === 'private' ? 'Личный диалог' : 'Система'">
          <span>{{ dialog.category === 'group' ? 'ГР' : dialog.category === 'private' ? 'ЛЧ' : 'СИ' }}</span>
        </span>
        <span class="min-w-0 flex-1">
          <span class="flex justify-between gap-2">
            <span class="truncate font-medium">{{ dialog.name }}</span>
            <span class="mx-muted shrink-0 text-[11px]">{{ formatTime(dialog.last_at) }}</span>
          </span>
          <span class="block mx-muted truncate">{{ dialog.last_preview || '—' }}</span>
          <span v-if="dialog.attention_count" class="mx-chip mx-chip-warn mt-1">Требует внимания: {{ dialog.attention_count }}</span>
        </span>
      </button>
      <button v-if="dialogsNext" class="mx-btn m-3" :disabled="dialogsLoading" @click="loadDialogs(true,true)">Ещё диалоги</button>
    </div>
  </aside>

  <!-- Лента -->
  <section class="mx-panel-main relative flex flex-col" aria-label="Лента сообщений">
    <header v-if="selected" class="flex items-center gap-2 px-4 py-2.5 shrink-0" style="border-bottom:1px solid var(--mx-border)">
      <button v-if="!roomy" class="mx-btn" style="padding:6px" aria-label="Показать список диалогов" @click="listOpen = true"><PanelLeftOpen :size="16" /></button>
      <div class="min-w-0 flex-1">
        <div class="font-semibold truncate">{{ dialogTitle }}</div>
        <div class="mx-muted text-[11px]">{{ dialogCategory }}<template v-if="CATEGORY_LABELS[selected.cat] && CATEGORY_LABELS[selected.cat] !== dialogCategory"> · {{ CATEGORY_LABELS[selected.cat] }}</template></div>
      </div>
      <span v-if="stale" class="mx-chip mx-chip-warn" title="Данные могут быть не самыми свежими"><RefreshCw :size="12" /> Данные могут быть не свежими</span>
      <button class="mx-btn text-xs" :disabled="recoveryBusy" @click="openRecovery">Повторить ошибки</button>
      <button v-if="!wide" class="mx-btn" style="padding:6px" aria-label="Открыть инспектор" @click="inspectorOpen = true"><PanelRightOpen :size="16" /></button>
    </header>

    <div v-if="!selected" class="flex-1 flex items-center justify-center">
      <button v-if="!roomy" class="mx-btn absolute top-3 left-3" @click="listOpen=true">Выбрать диалог</button>
      <EmptyState title="Выберите диалог" text="Слева — чаты, к которым у вас есть доступ. Сообщения и их обработка откроются здесь." />
    </div>
    <template v-else>
      <div v-if="recoveryOpen" class="mx-card m-3 p-3 space-y-2 text-sm" aria-label="Повторный анализ ошибок">
        <p v-if="recoveryError" role="alert" class="text-rose-300">{{ recoveryError }}</p>
        <p v-if="recoveryBusy" class="mx-muted">Проверяем сообщения…</p>
        <template v-if="recoveryProgress">
          <p>Найдено: {{ recoveryProgress.found }} · Поставлено: {{ recoveryProgress.scheduled }} · Ожидают: {{ recoveryProgress.pending }}</p>
          <p>Обработано: {{ recoveryProgress.succeeded }} · Требуют проверки: {{ recoveryProgress.needs_review }} · Ошибки: {{ recoveryProgress.failed }}</p>
          <p v-if="recoveryProgress.usage" class="mx-muted text-xs">Вызовов: {{ recoveryProgress.usage.calls }} · Токены: {{ recoveryProgress.usage.input_tokens ?? '—' }} вход / {{ recoveryProgress.usage.output_tokens ?? '—' }} выход</p>
          <p class="mx-muted text-xs">Уже обрабатываются: {{ recoveryProgress.already_processing }} · Изменились: {{ recoveryProgress.changed }} · Недоступны: {{ recoveryProgress.unavailable }}</p>
          <p v-if="recoveryProgress.error_code" role="alert" class="text-amber-200">Повтор остановлен. Проверьте основную модель и доступ, затем запустите заново.</p>
        </template>
        <template v-else-if="recoveryPreview">
          <p>Актуальных ошибок для повторного анализа: {{ recoveryPreview.found }}<span v-if="recoveryPreview.model"> · Модель: {{ recoveryPreview.model }}</span></p>
          <p class="mx-muted text-xs">Создадим новые попытки. Результаты с новыми фактами потребуют проверки перед записью в CRM. История прежних попыток сохранится.</p>
          <p v-if="recoveryPreview.blocker" class="text-amber-200">Сначала настройте и проверьте подключение основной чат-модели. <RouterLink class="mx-link" to="/settings?section=ai">Настройки ИИ</RouterLink></p>
          <button class="mx-btn mx-btn-primary" :disabled="recoveryBusy || !recoveryPreview.can_start" @click="startRecovery">Подтвердить повторный анализ</button>
        </template>
      </div>
      <div v-if="feedLoading && !feed.length" class="p-4"><Skeleton :rows="7" height="54px" /></div>
      <ErrorState v-else-if="feedError" :text="feedError" :on-retry="() => loadFeed(null)" />
      <EmptyState v-else-if="!feed.length" title="Сообщений пока нет" text="История этого диалога пуста или ещё обрабатывается." />
      <div v-else ref="feedBox" class="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-2" tabindex="0" aria-label="Сообщения" @scroll.passive="onFeedScroll">
        <div v-if="loadingOlder" class="mx-skeleton h-4 w-40 self-center" />
        <button v-if="!atBottom && newCount" class="mx-btn mx-btn-primary self-center sticky top-1 z-10" @click="scrollToBottom">
          <ChevronDown :size="14" /> Новые сообщения: {{ newCount }}
        </button>
        <template v-for="record in feed" :key="record.id">
          <div v-if="bubbleClass(record) === 'system'" class="self-center">
            <button
              class="mx-chip mx-chip-muted" :class="{ 'mx-chip-warn': record.attention }"
              :aria-current="selectedId === record.id ? 'true' : undefined" @click="selectRecord(record.id)"
            >
              {{ PURPOSE_LABELS[record.purpose] || KIND_LABELS[record.purpose] || 'Событие' }} · {{ formatTime(record.occurred_at) }}{{ record.time_is_estimated ? ' (оценочно)' : '' }}
            </button>
          </div>
          <button
            v-else
            class="max-w-[92%] sm:max-w-[80%] rounded-2xl px-3.5 py-2.5 text-left hover:brightness-110"
            :style="bubbleClass(record) === 'out'
              ? 'align-self:flex-end;background:var(--mx-card-3);border:1px solid var(--mx-border-2)'
              : 'align-self:flex-start;background:var(--mx-card);border:1px solid var(--mx-border)'"
            :data-record-id="record.id" :class="selectedId === record.id ? 'ring-2 ring-[var(--mx-accent)]' : ''"
            @click="selectRecord(record.id)"
          >
            <div class="flex gap-2 items-center text-[11px] mx-muted mb-1">
              <span>{{ record.sender_name || PURPOSE_LABELS[record.purpose] || record.purpose }}</span>
              <span>·</span>
              <span>{{ bubbleClass(record) === 'out' ? 'Исходящее' : 'Входящее' }}</span>
              <span>·</span>
              <span>{{ formatTime(record.occurred_at) }}{{ record.time_is_estimated ? ' (оценочно)' : '' }}</span>
            </div>
            <div style="font-size:14px;white-space:pre-wrap;word-break:break-word">{{ record.body }}</div>
            <div class="flex gap-1.5 flex-wrap mt-1.5">
              <StatusBadge :state="record.statuses?.analysis || 'not_applicable'" />
              <StatusBadge v-if="record.statuses?.clarification && record.statuses.clarification !== 'not_applicable'" :state="record.statuses.clarification" />
              <StatusBadge v-if="record.statuses?.crm && record.statuses.crm !== 'not_required'" :state="record.statuses.crm" />
            </div>
          </button>
        </template>
      </div>
    </template>
  </section>

  <!-- Инспектор -->
  <template v-if="!wide && inspectorOpen"><div class="mx-overlay" @click="inspectorOpen = false" /></template>
  <aside class="mx-panel-inspector flex flex-col" :class="{ 'mx-inspector-hidden': !wide && !inspectorOpen }" data-inspector :role="!wide ? 'dialog' : undefined" :aria-modal="!wide && inspectorOpen ? true : undefined" aria-label="Инспектор">
    <div class="flex flex-wrap items-center gap-1 px-3 pt-3 shrink-0">
      <button
        v-for="item in ([{ id: 'decision', label: 'Решение' }, { id: 'history', label: 'История' }, { id: 'trace', label: 'Диагностика ИИ' }] as const)"
        :key="item.id" class="mx-tab" :class="{ 'mx-tab-active': tab === item.id }"
        :aria-current="tab === item.id ? 'page' : undefined"
        @click="patchQuery({ tab: item.id === 'decision' ? null : item.id })"
      >{{ item.label }}</button>
      <button v-if="!wide" class="mx-btn ml-auto" style="padding:4px" aria-label="Закрыть инспектор" @click="inspectorOpen = false"><X :size="15" /></button>
    </div>
    <div class="flex-1 overflow-y-auto p-3 flex flex-col gap-3">
      <EmptyState v-if="!selectedId" title="Сообщение не выбрано" text="Откройте сообщение в ленте — здесь появятся решение, история и диагностика." />
      <template v-else-if="detail">
        <details class="mx-card p-3"><summary>Исходное сообщение</summary><p class="whitespace-pre-wrap break-words mt-2 text-sm">{{ detail.body }}</p></details>
        <div class="mx-card p-3">
          <div class="text-[11px] mx-muted mb-1">Статусы · сообщение #{{ detail.id }}</div>
          <div class="flex flex-col gap-1.5">
            <div class="flex items-center gap-2"><span class="mx-muted w-28 shrink-0">Анализ</span><StatusBadge :state="detail.statuses.analysis" /></div>
            <div class="flex items-center gap-2"><span class="mx-muted w-28 shrink-0">Уточнение</span><StatusBadge :state="detail.statuses.clarification" /></div>
            <div class="flex items-center gap-2"><span class="mx-muted w-28 shrink-0">WhatsApp</span><StatusBadge :state="detail.statuses.delivery_whatsapp" /></div>
            <div class="flex items-center gap-2"><span class="mx-muted w-28 shrink-0">CRM</span><StatusBadge :state="detail.statuses.crm" /></div>
          </div>
          <div v-if="detail.statuses.attention_reasons?.length" class="mt-2 flex flex-wrap gap-1.5">
            <span v-for="reason in detail.statuses.attention_reasons" :key="reason" class="mx-chip mx-chip-warn">▲ {{ statusLabel(reason) || reason }}</span>
          </div>
          <div class="flex gap-2 mt-3 flex-wrap">
            <button
              v-if="detail.capabilities.can_reanalyze" class="mx-btn" :disabled="reanalyzeState === 'sent'"
              @click="reanalyze"
            >
              <RefreshCw :size="13" />
              {{ reanalyzeState === 'idle' ? 'Повторить анализ' : reanalyzeState === 'confirm' ? 'Подтвердите повтор' : 'Новая попытка поставлена' }}
            </button>
          </div>
        </div>

        <!-- Решение -->
        <template v-if="tab === 'decision'">
          <ErrorState v-if="chainError" :text="chainError" :on-retry="loadTab" />
          <template v-else-if="chain">
            <div v-if="!chain.nodes.length" class="mx-card p-3 mx-muted">Для этого сообщения нет связанных решений и внешних сущностей. Вероятно, оно не относится к сделкам — без CRM-действий.</div>
            <div v-for="node in chain.nodes" :key="node.kind + node.id" class="mx-card p-3">
              <div class="flex items-center justify-between gap-2">
                <div class="font-medium">{{ KIND_LABELS[node.kind] || node.label }}</div>
                <StatusBadge :state="node.state" />
              </div>
              <div class="mx-muted mt-0.5">{{ node.label }}</div>
              <button v-if="node.allowed && node.href" class="mx-link text-[12px] mt-1.5" @click="gotoNode(node)">Открыть в разделе →</button>
              <span v-else-if="!node.allowed" class="mx-muted text-[12px] mt-1.5 block">Нет доступа к этому объекту</span>
            </div>
          </template>
          <Skeleton v-else :rows="4" height="52px" />
        </template>

        <!-- История -->
        <template v-else-if="tab === 'history'">
          <ErrorState v-if="chainError" :text="chainError" :on-retry="loadTab" />
          <template v-else-if="chain">
            <div v-if="!chain.timeline.length" class="mx-card p-3 mx-muted">Событий журнала для этого сообщения нет.</div>
            <ol v-else class="relative ml-2" style="border-left:1px solid var(--mx-border-2)">
              <li v-for="step in timelineOrdered" :key="step.id" class="pl-4 pb-4 relative">
                <span class="absolute -left-[5px] top-1 w-2 h-2 rounded-full" style="background:var(--mx-accent)" />
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="font-medium">{{ KIND_LABELS[step.kind] || step.kind }}</span>
                  <StatusBadge :state="step.state" />
                </div>
                <div class="mx-muted text-[11px]">{{ formatTime(step.occurred_at) }}{{ step.time_is_estimated ? ' · оценочное время' : '' }}{{ step.parent_event_id ? ` · событие #${step.parent_event_id} → #${step.id}` : '' }}</div>
              </li>
            </ol>
            <button v-if="chain.next_cursor" class="mx-btn" @click="moreEvents">Следующие события</button>
          </template>
          <Skeleton v-else :rows="5" height="36px" />
        </template>

        <!-- Диагностика ИИ -->
        <template v-else>
          <div v-if="!detail.capabilities.can_trace" class="mx-card p-3 mx-muted">Диагностика ИИ доступна руководителю команды.</div>
          <template v-else>
            <ErrorState v-if="tracesError" :text="tracesError" :on-retry="loadTab" />
            <Skeleton v-else-if="traces === null" :rows="4" height="64px" />
            <EmptyState v-else-if="!traces.length" title="Попыток анализа нет" text="Для этого сообщения ещё не выполнялся анализ." />
            <button v-if="tracesCursor" class="mx-btn" @click="moreTraces">Более ранние попытки</button>
            <div v-for="row in traces" :key="row.id" :data-trace-id="row.id" class="mx-card p-3">
              <div class="flex items-center justify-between gap-2">
                <div class="font-medium">Попытка №{{ row.attempt_no }}</div>
                <StatusBadge :state="row.status" />
              </div>
              <div class="mx-muted text-[12px] mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                <span v-if="row.pipeline_action">Стадия: {{ row.pipeline_action }}</span>
                <span v-if="row.model_version">Модель: {{ row.model_version }}</span>
                <span v-if="row.prompt_version">Prompt: {{ row.prompt_version }}</span>
                <span v-if="row.error_code">Код: {{ row.error_code }}</span>
              </div>
              <FullHistoryProgress :value="row.context_metadata.full_history" /><EntityStageProgress :value="row.context_metadata.entity_pipeline" /><TraceContextPanel v-if="row.has_context" :record-id="detail.id" :trace-id="row.id" />
              <EntityStageProgress v-for="(progress, key) in entityReconciliations(row.context_metadata.entity_reconciliations)" :key="key" :value="progress" label="Сопоставление с обновлённым справочником" />
              <details class="mt-2"><summary>Контекст и извлечённые факты</summary><pre class="whitespace-pre-wrap break-words text-xs">{{ row.context_metadata }}</pre><pre class="whitespace-pre-wrap break-words text-xs">{{ row.ai_extracted_facts }}</pre></details>
              <div v-if="row.result_summary" class="mt-1.5">{{ row.result_summary }}</div>
              <div v-if="row.bitrix_matched_deal_id" class="mx-muted text-[12px] mt-1">
                Сделка: {{ row.bitrix_matched_deal_id }}{{ row.bitrix_deal_title ? ` · ${row.bitrix_deal_title}` : '' }}{{ row.bitrix_deal_stage ? ` · ${row.bitrix_deal_stage}` : '' }}
              </div>
            </div>
          </template>
        </template>
      </template>
      <ErrorState v-else-if="detailError" :text="detailError" :on-retry="loadDetail" />
      <Skeleton v-else :rows="3" height="48px" />
    </div>
  </aside>
</template>
