<template>
  <section class="chat-shell" aria-label="Аналитический диалог">
    <header class="workspace-header"><div><p class="eyebrow">MAZORY · АНАЛИТИКА</p><h1>Диалог и рабочая область</h1></div><button class="chat-button" @click="$emit('newConversation')"><Plus :size="16" /> Новый диалог</button></header>
    <div class="context-bar">
      <label class="select-wrap"><span class="sr-only">Диалоги</span><select aria-label="Диалоги" :value="conversationId ?? ''" @change="chooseConversation"><option value="" disabled>Текущий диалог</option><option v-for="c in conversations" :key="c.id" :value="c.id">{{ c.title }}</option></select><ChevronDown :size="16" /></label>
      <button class="chat-button" @click="showFilters = !showFilters"><SlidersHorizontal :size="16" /> Условия нового запроса</button>
      <span class="muted">Уточнения сохраняют условия выбранного результата</span>
    </div>
    <div v-if="showFilters" class="scope-grid">
      <label>Команда<span class="select-wrap"><select v-model="scope.team_id" aria-label="Команда"><option :value="undefined">Все доступные</option><option v-for="t in directory.teams" :key="t.id" :value="t.id">{{ t.name }}</option></select><ChevronDown :size="16" /></span></label>
      <label>Менеджер<span class="select-wrap"><select v-model="scope.manager_id" aria-label="Менеджер"><option :value="undefined">Все доступные</option><option v-for="p in directory.profiles" :key="p.id" :value="p.id">{{ p.full_name }}</option></select><ChevronDown :size="16" /></span></label>
      <label>Проект<span class="select-wrap"><select v-model="scope.project_id" aria-label="Проект"><option :value="undefined">Все доступные</option><option v-for="p in directory.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select><ChevronDown :size="16" /></span></label>
      <label>Период<span class="select-wrap"><select :value="period" aria-label="Период нового запроса" @change="$emit('changePeriod', ($event.target as HTMLSelectElement).value)"><option value="this_month">Текущий месяц</option><option value="last_month">Прошлый месяц</option><option value="quarter">Квартал</option><option value="year">Год</option></select><ChevronDown :size="16" /></span></label>
      <label>Валюта<span class="select-wrap"><select v-model="scope.currency" aria-label="Валюта"><option>KZT</option><option>USD</option><option>EUR</option><option>RUB</option></select><ChevronDown :size="16" /></span></label>
    </div>
    <div class="workspace-layout">
      <div class="dialogue-column">
        <h2>Диалог <span class="muted">{{ turns.length ? `${turns.length} запросов` : '' }}</span></h2>
        <div ref="transcript" class="transcript" aria-live="polite" aria-relevant="additions text">
          <div v-if="!turns.length" class="welcome-answer"><Sparkles :size="20" /><p>Привет! Помогу разобраться в поступлениях, сделках и задачах. Что посмотрим?</p><div class="suggestions"><button v-for="p in initialPrompts" :key="p" class="chat-button" @click="$emit('submit', p)">{{ p }} <ArrowUpRight :size="14" /></button></div></div>
          <article v-for="turn in turns" :key="turn.id" class="turn" :class="{ selected: selectedTurnId === turn.id }" :data-testid="`chat-turn-${turn.sequence}`">
            <div class="user-message"><span class="message-label">Вы</span><p>{{ turn.user_text }}</p></div>
            <div class="assistant-message"><span class="message-label"><Sparkles :size="14" /> Mazory</span>
              <p v-if="['queued', 'running'].includes(turn.state)" role="status" class="muted"><LoaderCircle class="spinner" :size="16" /> {{ turn.state === 'running' ? 'Считаю и готовлю ответ…' : turn.parent_turn_id ? 'Жду завершения исходного запроса…' : 'Запрос в очереди…' }} <button v-if="turn.operation_id" class="text-button" @click="$emit('cancel', turn.operation_id)">Отменить</button></p>
              <AnswerMarkdown v-if="turn.answer_document" :text="turn.answer_document.markdown" data-testid="chat-response" />
              <p v-if="turn.error" class="turn-error" role="alert">{{ turn.error }}</p>
              <div v-if="turn.artifacts.length" class="result-links"><button v-for="a in turn.artifacts" :key="a.id" class="chat-button" :aria-pressed="artifact?.id === a.id" @click="$emit('selectTurn', turn.id, a.id)"><ChartNoAxesCombined :size="16" /> {{ a.title }} <span v-if="!a.available">· пересчитать</span></button></div>
              <button v-else-if="turn.state === 'succeeded'" class="text-button" @click="$emit('selectTurn', turn.id)">Продолжить этот ответ</button>
              <div v-if="turn.answer_document?.suggested_actions.length" class="suggestions"><button v-for="action in turn.answer_document.suggested_actions.slice(0, 3)" :key="action.label" class="chat-button" @click="$emit('followup', turn.id, action.artifact_id, action.prompt, action.patch)">{{ action.label }} <ArrowUpRight :size="14" /></button></div>
            </div>
          </article>
        </div>
      </div>
      <div class="result-column">
        <div class="result-heading"><h2>Рабочая область</h2><span v-if="artifact" class="version-pill">Версия {{ turns.find(t => t.artifacts.some(a => a.id === artifact?.id))?.sequence || 1 }}</span></div>
        <template v-if="presentation">
          <h3 class="result-title">{{ presentation.title }}</h3>
          <div class="result-toolbar"><button class="chat-button" :aria-pressed="!table" @click="table = false"><ChartNoAxesCombined :size="16" /> График</button><button class="chat-button" :aria-pressed="table" @click="table = true"><Table2 :size="16" /> Таблица</button><label v-if="!table && canChangeType" class="select-wrap type-select"><select aria-label="Тип графика" v-model="chartKind"><option value="bar">Столбцы</option><option value="line">Линия</option><option value="area">Область</option></select><ChevronDown :size="16" /></label></div>
          <div class="result-controls"><label v-if="groupOptions.length">Группировка<span class="select-wrap"><select aria-label="Группировка" :value="groupValue" @change="group"><option v-for="o in groupOptions" :key="o.value" :value="o.value">{{ o.label }}</option></select><ChevronDown :size="16" /></span></label><label v-if="canSort">Сортировка<span class="select-wrap"><select aria-label="Сортировка" :value="sortValue" @change="sort"><option v-if="hasDates" value="date_asc">От ранних к поздним</option><option v-if="hasDates" value="date_desc">От поздних к ранним</option><option value="value_desc">По убыванию значения</option><option value="value_asc">По возрастанию значения</option></select><ChevronDown :size="16" /></span></label></div>
          <PresentationRenderer :document="displayDocument!" @open-source="$emit('openSource', $event)" />
          <div class="suggestions"><button class="chat-button" @click="focusComposer('Добавь ')" ><Plus :size="16" /> Добавить показатель</button><button v-if="hasDates" class="chat-button" @click="$emit('submit', 'Добавь месячный план к этому графику')">Добавить план <ArrowUpRight :size="14" /></button><button class="chat-button" @click="$emit('submit', 'Объясни этот результат')">Объяснить результат</button></div>
        </template>
        <div v-else class="result-empty"><ChartNoAxesCombined :size="32" /><p>{{ artifact?.message || 'Здесь появится выбранный график или таблица.' }}</p><p class="muted">Попросите построить график или выберите результат в диалоге.</p><button v-if="artifact && !artifact.available" class="chat-button" @click="$emit('submit', 'Пересчитай выбранный результат по тем же условиям')">Пересчитать</button></div>
      </div>
    </div>
    <p v-if="error" role="alert" class="turn-error">{{ error }}</p>
    <form class="workspace-composer" @submit.prevent="submit">
      <label for="analytical-prompt" class="message-label">{{ turns.some(t => t.id === selectedTurnId && ['queued', 'running'].includes(t.state)) ? 'Дополнение к выполняемому запросу' : selectedTurnId ? 'Уточнение к выбранному ответу' : 'Ваш вопрос' }} <span v-if="isGenerating" class="muted">· можно дополнить запрос во время расчёта</span></label>
      <textarea id="analytical-prompt" ref="composer" v-model="prompt" maxlength="4000" rows="2" placeholder="Добавь план или измени группировку…" @keydown.enter.exact.prevent="submit" />
      <div class="composer-footer"><span class="muted">Enter — отправить · Shift + Enter — новая строка</span><button class="send-button" :disabled="!prompt.trim()" type="submit">Отправить <ArrowUp :size="18" /></button></div>
    </form>
  </section>
</template>
<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { ChevronDown, Plus, SlidersHorizontal, Sparkles, ArrowUpRight, ArrowUp, ChartNoAxesCombined, Table2, LoaderCircle } from 'lucide-vue-next'
import type { AnalyticsTurn, ConversationRef, Artifact, QueryPatch } from '../types/dialogue'
import type { PresentationDocument } from '../types/presentation'
import type { KpiFilters, Directory } from '../types/platform'
import { api } from '../composables/api'
import AnswerMarkdown from './AnswerMarkdown.vue'
import PresentationRenderer from '../presentation/PresentationRenderer.vue'
const props = defineProps<{ turns: AnalyticsTurn[]; conversations: ConversationRef[]; conversationId: number | null; selectedTurnId: number | null; artifact: Artifact | null; presentation: PresentationDocument | null; isGenerating: boolean; error: string; period: string }>()
const emit = defineEmits<{ submit: [prompt: string, patch?: QueryPatch]; followup: [turnId: number, artifactId: number | undefined, prompt: string, patch?: QueryPatch]; selectTurn: [id: number, artifactId?: number]; loadConversation: [id: number]; newConversation: []; cancel: [operationId: number]; openSource: [id: number]; changeFilters: [filters: KpiFilters]; changePeriod: [period: string] }>()
const prompt = ref(''), composer = ref<HTMLTextAreaElement>(), transcript = ref<HTMLDivElement>(), table = ref(false), chartKind = ref<'bar' | 'line' | 'area'>('bar'), showFilters = ref(false)
const scope = ref<KpiFilters>({ currency: 'KZT' }), directory = ref<Directory>({ teams: [], profiles: [], projects: [] })
const initialPrompts = ['Покажи поступления по месяцам', 'Покажи сделки CRM по стадиям', 'Какие обещания просрочены?']
onMounted(async () => { try { directory.value = await api<Directory>('/directory/') } catch { /* API reports access errors. */ } })
watch(scope, value => emit('changeFilters', { ...value }), { deep: true })
watch(() => props.turns.length, async () => { await nextTick(); if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight })
watch(() => props.presentation, doc => { table.value = false; const kind = doc?.blocks[0]?.kind; chartKind.value = kind === 'line' || kind === 'area' ? kind : 'bar' })
const dataset = computed(() => props.presentation ? Object.values(props.presentation.datasets)[0] : undefined)
const source = computed(() => dataset.value?.normalized_query.dataset)
const canChangeType = computed(() => props.presentation?.blocks.every(b => ['bar', 'line', 'area'].includes(b.kind)))
const hasDates = computed(() => dataset.value?.columns.some(c => c.type === 'date'))
const canSort = computed(() => source.value !== 'commitment_records')
const groupOptions = computed(() => {
  if (source.value === 'commitment_records') return []
  if (source.value === 'combined') return hasDates.value ? [{ value: 'month', label: 'По месяцам' }] : []
  const dated = ['payments', 'messages', 'commitments'].includes(String(source.value))
  return [...(dated ? [{ value: 'day', label: 'По дням' }, { value: 'week', label: 'По неделям' }, { value: 'month', label: 'По месяцам' }, { value: 'quarter', label: 'По кварталам' }, { value: 'year', label: 'По годам' }] : []),
    ...(source.value !== 'messages' ? [{ value: 'manager', label: source.value === 'crm_projects' ? 'По ответственным CRM' : 'По менеджерам' }] : []),
    ...(['crm_projects', 'projects', 'commitments'].includes(String(source.value)) ? [{ value: 'status', label: 'По стадиям / статусам' }] : [])]
})
const groupValue = computed(() => { const dims = dataset.value?.normalized_query.dimensions as string[] | undefined; const date = dims?.find(d => d.includes('_day') || d.includes('_week') || d.includes('_month') || d.includes('_quarter') || d.includes('_year')); return date?.split('_').at(-1) || (dims?.includes('status') ? 'status' : 'manager') })
const sortValue = computed(() => { const orders = dataset.value?.normalized_query.order_by as { field: string; direction: string }[] | undefined; const first = orders?.[0]; return first ? `${dataset.value?.columns.find(c => c.name === first.field)?.type === 'date' ? 'date' : 'value'}_${first.direction}` : hasDates.value ? 'date_asc' : 'value_desc' })
const displayDocument = computed<PresentationDocument | null>(() => props.presentation ? { ...props.presentation, blocks: props.presentation.blocks.map(b => table.value ? { ...b, kind: 'table', encoding: {}, columns: props.presentation!.datasets[b.dataset_id]!.columns.map(c => c.name), steps: undefined } : canChangeType.value ? { ...b, kind: chartKind.value } : b) } : null)
function submit() { const text = prompt.value.trim(); if (!text) return; prompt.value = ''; emit('submit', text) }
function focusComposer(text: string) { prompt.value = text; composer.value?.focus() }
function group(event: Event) { const value = (event.target as HTMLSelectElement).value as QueryPatch['grouping']; emit('submit', `Измени группировку: ${groupOptions.value.find(o => o.value === value)?.label}`, { grouping: value }) }
function sort(event: Event) { const select = event.target as HTMLSelectElement; emit('submit', `Сортировка: ${select.selectedOptions[0]?.text}`, { sort: select.value as QueryPatch['sort'] }) }
function chooseConversation(event: Event) { const select = event.target as HTMLSelectElement; const c = props.conversations.find(c => String(c.id) === select.value); if (c) emit('loadConversation', c.id) }
</script>
<style scoped>
.chat-shell { --surface:#101827; --border:#2a354a; --muted:#a0acc1; container-type:inline-size; container-name:analytical-chat; width:100%; max-width:1200px; margin:0 auto; padding:24px; color:#e7eaf3; }
.workspace-header,.context-bar,.result-heading,.result-toolbar,.composer-footer { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
.workspace-header h1 { font-size:24px; font-weight:650; margin:4px 0 20px; }
.eyebrow { font-size:11px; letter-spacing:.12em; color:#aaa7ff; }
.context-bar { justify-content:flex-start; margin-bottom:20px; }
.context-bar > .select-wrap { max-width:360px; flex:1 1 200px; }
.workspace-layout { display:grid; grid-template-columns:minmax(0,1fr); gap:20px; align-items:start; }
.dialogue-column,.result-column { min-width:0; padding:20px; border:1px solid var(--border); border-radius:20px; background:#0c121eee; }
.dialogue-column h2,.result-heading h2 { font-size:15px; font-weight:600; }
.transcript { margin-top:20px; max-height:min(68vh,760px); overflow-y:auto; padding-right:4px; }
.turn { margin:0 0 24px; border-left:2px solid transparent; padding-left:12px; overflow-wrap:anywhere; }
.turn.selected { border-left-color:#9390ff; }
.user-message { padding:12px 16px; background:#1b2339; border-radius:14px; margin-bottom:16px; }
.message-label { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); margin-bottom:8px; flex-wrap:wrap; }
.assistant-message { font-size:14px; }
.assistant-message [role=status] { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.result-links,.suggestions { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }
.chat-button,.send-button { display:inline-flex; align-items:center; justify-content:center; gap:8px; border:1px solid var(--border); border-radius:12px; padding:10px 14px; font-size:13px; min-height:40px; max-width:100%; overflow-wrap:anywhere; }
.chat-button { background:var(--surface); color:#b2afff; text-align:left; }
.chat-button:hover,.chat-button[aria-pressed=true] { border-color:#9390ff; background:#242340; }
.chat-button:focus-visible,.send-button:focus-visible,select:focus-visible,textarea:focus-visible { outline:2px solid #aaa7ff; outline-offset:3px; }
.text-button { color:#aaa7ff; text-decoration:underline; font-size:12px; }
.muted { color:var(--muted); font-size:12px; }
.result-title { margin:16px 0; font-size:20px; font-weight:600; }
.version-pill { font-size:11px; border-radius:20px; padding:5px 10px; background:#242340; color:#b2afff; }
.result-controls,.scope-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr)); gap:12px; margin:16px 0; }
.result-controls label,.scope-grid label { min-width:0; font-size:13px; color:var(--muted); }
.select-wrap { display:block; position:relative; min-width:0; margin-top:6px; }
.select-wrap select { appearance:none; width:100%; min-width:0; border:1px solid var(--border); border-radius:12px; min-height:44px; padding:10px 44px 10px 14px; color:#e7eaf3; background:var(--surface); font-size:14px; text-overflow:ellipsis; }
.select-wrap > svg { position:absolute; right:14px; top:50%; transform:translateY(-50%); pointer-events:none; }
.type-select { min-width:140px; }
.result-empty { min-height:240px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:16px; text-align:center; color:var(--muted); }
.workspace-composer { width:100%; min-width:0; margin-top:20px; padding:20px; border:1px solid var(--border); border-radius:20px; background:var(--surface); }
.workspace-composer textarea { display:block; width:100%; min-width:0; min-height:70px; resize:vertical; background:transparent; border:0; color:#e7eaf3; font-size:16px; padding:0; }
.workspace-composer textarea::placeholder { color:#8390a7; }
.composer-footer { margin-top:12px; }
.send-button { background:#9390ff; color:#090d17; border-color:#9390ff; font-weight:600; }
.send-button:disabled { opacity:.4; }
.turn-error { color:#fda4af; margin:12px 0; font-size:14px; }
.spinner { animation:rotate 1s linear infinite; }
@keyframes rotate { to { transform:rotate(360deg); } }
@container analytical-chat (min-width:960px) { .workspace-layout { grid-template-columns:minmax(300px,.9fr) minmax(0,1.6fr); } }
@container analytical-chat (max-width:440px) { .dialogue-column,.result-column,.workspace-composer { padding:14px; border-radius:16px; } .workspace-header h1 { font-size:20px; } .result-controls { grid-template-columns:minmax(0,1fr); } .composer-footer .muted { display:none; } .send-button { margin-left:auto; } }
@media(max-width:500px) { .chat-shell { padding:14px; } .select-wrap select { font-size:16px; } }
@media(prefers-reduced-motion:reduce) { .spinner { animation:none; } }
</style>
