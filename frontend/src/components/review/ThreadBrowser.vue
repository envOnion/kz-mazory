<template>
  <section class="space-y-4">
    <!-- Верхний заголовок и сводная панель -->
    <header class="flex flex-wrap items-center justify-between gap-3 pb-2 border-b border-slate-800">
      <div>
        <h2 class="text-xl font-bold text-white flex items-center gap-2">
          <span>📁</span> Дерево диалогов и тем переписки
        </h2>
        <p class="text-xs text-slate-400 mt-1">
          Иерархическая структура: Компания Bitrix → Сделка/Объект → Корневой диалог → Поддиалоги.
        </p>
      </div>

      <!-- Сводные индикаторы -->
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <div class="px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-700/80">
          <span class="text-slate-400">Всего тем:</span>
          <strong class="ml-1 text-white">{{ stats.total }}</strong>
        </div>
        <div class="px-2.5 py-1 rounded-lg bg-amber-950/40 border border-amber-500/30">
          <span class="text-amber-400">⏳ В процессе (open):</span>
          <strong class="ml-1 text-amber-300">{{ stats.open }}</strong>
        </div>
        <div class="px-2.5 py-1 rounded-lg bg-emerald-950/40 border border-emerald-500/30">
          <span class="text-emerald-400">✅ Завершено (ready):</span>
          <strong class="ml-1 text-emerald-300">{{ stats.ready }}</strong>
        </div>
        <div v-if="stats.commitments" class="px-2.5 py-1 rounded-lg bg-indigo-950/40 border border-indigo-500/30">
          <span class="text-indigo-400">🎯 Обязательств:</span>
          <strong class="ml-1 text-indigo-300">{{ stats.commitments }}</strong>
        </div>
      </div>
    </header>

    <!-- Панель управления и запуска разбора для руководителя -->
    <section v-if="lead && chats.length" class="panel flex flex-wrap items-center justify-between gap-3 text-sm">
      <div class="flex items-center gap-2">
        <label class="text-slate-300 font-medium">Чат WhatsApp:</label>
        <select v-model="chatId" class="field py-1">
          <option v-for="chat in chats" :key="chat.id" :value="chat.id">{{ chat.name }}</option>
        </select>
        <button class="btn text-xs py-1.5" :disabled="loading || !chatId" @click="rebuild">
          Разобрать историю по темам
        </button>
      </div>
      <p v-if="notice" role="status" class="text-xs text-emerald-300 font-medium">{{ notice }}</p>
    </section>

    <!-- Тулбар фильтрации и поиска -->
    <div class="flex flex-wrap items-center justify-between gap-3 p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-xs">
      <!-- Фильтры статуса -->
      <div class="flex flex-wrap items-center gap-1.5">
        <span class="text-slate-400 mr-1">Статус:</span>
        <button
          class="btn py-1 px-2.5 text-xs transition-colors"
          :class="statusFilter === 'all' ? 'bg-indigo-600 border-indigo-400 text-white font-medium' : 'text-slate-300'"
          @click="setStatusFilter('all')"
        >
          Все ({{ serverStats.total }})
        </button>
        <button
          class="btn py-1 px-2.5 text-xs transition-colors"
          :class="statusFilter === 'open' ? 'bg-amber-600 border-amber-400 text-white font-medium' : 'text-slate-300'"
          @click="setStatusFilter('open')"
        >
          ⏳ В процессе ({{ serverStats.open }})
        </button>
        <button
          class="btn py-1 px-2.5 text-xs transition-colors"
          :class="statusFilter === 'ready' ? 'bg-emerald-600 border-emerald-400 text-white font-medium' : 'text-slate-300'"
          @click="setStatusFilter('ready')"
        >
          ✅ Завершенные ({{ serverStats.ready }})
        </button>
      </div>

      <!-- Действия со структурой и поиск -->
      <div class="flex flex-wrap items-center gap-2">
        <button class="btn py-1 px-2 text-xs" @click="toggleAllFolders(true)">Развернуть всё</button>
        <button class="btn py-1 px-2 text-xs" @click="toggleAllFolders(false)">Свернуть</button>
        <form class="flex items-center gap-1" @submit.prevent="load(1)">
          <input
            v-model="search"
            class="field py-1 px-2 text-xs w-48 sm:w-64"
            placeholder="Поиск по теме, компании, сделке..."
          />
          <button class="btn py-1 px-2 text-xs" :disabled="loading">Найти</button>
        </form>
      </div>
    </div>

    <!-- Сообщения об ошибках и загрузке -->
    <p v-if="error" role="alert" class="panel text-rose-300 text-sm">{{ error }}</p>
    <p v-if="loading && !items.length" role="status" class="text-sm text-slate-400">Загрузка структуры диалогов…</p>

    <!-- ДВУХПАНЕЛЬНЫЙ ИНТЕРФЕЙС -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 items-start">
      <!-- ЛЕВАЯ КОЛОНКА: ДЕРЕВО ПАПОК ДИАЛОГОВ (5 колонок на lg) -->
      <aside class="lg:col-span-5 space-y-3">
        <div v-if="!treeCompanies.length && !loading" class="panel text-sm text-slate-400">
          Темы диалогов не найдены.
        </div>

        <!-- Уровень 1: Компании Bitrix -->
        <article
          v-for="company in treeCompanies"
          :key="company.key"
          class="panel p-0 overflow-hidden border-slate-800"
        >
          <!-- Заголовок компании -->
          <div
            class="flex items-center justify-between p-3 bg-slate-900/90 hover:bg-slate-800/80 cursor-pointer select-none transition-colors border-b border-slate-800/60"
            @click="toggleFolder(company.key)"
          >
            <div class="flex items-center gap-2 min-w-0">
              <span class="text-base text-indigo-400">
                {{ isFolderOpen(company.key) ? '📂' : '📁' }}
              </span>
              <div class="min-w-0">
                <h3 class="font-semibold text-sm text-white truncate" :title="company.name">
                  {{ company.name }}
                </h3>
                <p class="text-[11px] text-slate-400">
                  {{ company.projects.length }} сделок · {{ company.threadCount }} диалогов
                </p>
              </div>
            </div>
            <span class="text-xs text-slate-400 px-2 py-0.5 rounded bg-slate-800">
              {{ isFolderOpen(company.key) ? '▲' : '▼' }}
            </span>
          </div>

          <!-- Содержимое компании: Сделки / Объекты -->
          <div v-if="isFolderOpen(company.key)" class="p-2 space-y-2 bg-slate-950/40">
            <!-- Уровень 2: Сделки Bitrix -->
            <div
              v-for="project in company.projects"
              :key="project.key"
              class="rounded-lg border border-slate-800/80 bg-slate-900/40 overflow-hidden"
            >
              <!-- Заголовок сделки -->
              <div
                class="flex items-center justify-between p-2.5 hover:bg-slate-800/60 cursor-pointer select-none transition-colors text-xs"
                @click="toggleFolder(project.key)"
              >
                <div class="flex items-center gap-2 min-w-0">
                  <span class="text-amber-400 font-bold">💼</span>
                  <div class="min-w-0">
                    <span class="font-medium text-slate-200 truncate block" :title="project.name">
                      {{ project.name }}
                    </span>
                  </div>
                </div>
                <div class="flex items-center gap-1.5 text-[11px]">
                  <span class="text-slate-400">{{ project.threads.length }} диалогов</span>
                  <span class="text-slate-500">{{ isFolderOpen(project.key) ? '▲' : '▼' }}</span>
                </div>
              </div>

              <!-- Содержимое сделки: Список корневых диалогов -->
              <div v-if="isFolderOpen(project.key)" class="p-2 space-y-1.5 border-t border-slate-800/60">
                <!-- Уровень 3: Корневые диалоги -->
                <div
                  v-for="thread in project.threads"
                  :key="thread.id"
                  class="p-2 rounded-lg border cursor-pointer transition-all text-xs space-y-1"
                  :class="[
                    detail?.id === thread.id
                      ? 'border-indigo-500 bg-indigo-950/30'
                      : 'border-slate-800 hover:border-slate-700 bg-slate-900/60 hover:bg-slate-900'
                  ]"
                  @click="open(thread.id)"
                >
                  <div class="flex items-start justify-between gap-2">
                    <div class="flex items-center gap-1.5 min-w-0">
                      <span class="text-sm">{{ thread.children_count ? '🗂️' : '💬' }}</span>
                      <strong class="font-medium text-slate-100 truncate" :title="thread.topic">
                        #{{ thread.id }}: {{ thread.topic }}
                      </strong>
                      <span v-if="thread.is_subscribed" class="text-xs shrink-0" title="Вы подписаны на эту тему">🔔</span>
                    </div>
                    <span
                      class="px-1.5 py-0.5 rounded text-[10px] whitespace-nowrap font-medium"
                      :class="thread.state === 'open' ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40' : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'"
                    >
                      {{ labels[thread.state] || thread.state }}
                    </span>
                  </div>

                  <div class="flex flex-wrap items-center justify-between text-[11px] text-slate-400 pt-0.5">
                    <span>
                      {{ thread.messages_count ? `${thread.messages_count} репл.` : '' }}
                      {{ thread.children_count ? ` · ${thread.children_count} поддиалогов` : '' }}
                    </span>
                    <span v-if="thread.total_amount" class="text-emerald-400 font-semibold">
                      ₸ {{ formatMoney(thread.total_amount) }}
                    </span>
                  </div>

                  <!-- Уровень 4: Дочерние поддиалоги (если есть) -->
                  <div v-if="subthreadsByParent[thread.id]?.length" class="mt-2 pl-3 border-l-2 border-slate-700 space-y-1">
                    <div
                      v-for="sub in subthreadsByParent[thread.id]"
                      :key="sub.id"
                      class="p-1.5 rounded hover:bg-slate-800 cursor-pointer flex items-center justify-between gap-1 text-[11px]"
                      :class="detail?.id === sub.id ? 'bg-indigo-900/40 text-indigo-200' : 'text-slate-300'"
                      @click.stop="open(sub.id)"
                    >
                      <span class="truncate flex items-center gap-1">
                        <span class="text-slate-500">↳</span> #{{ sub.id }}: {{ sub.topic }}
                      </span>
                      <span
                        class="text-[9px] px-1 rounded"
                        :class="sub.state === 'open' ? 'text-amber-400 bg-amber-950/60' : 'text-emerald-400 bg-emerald-950/60'"
                      >
                        {{ sub.state }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </article>

        <!-- Навигация страниц пагинации -->
        <nav v-if="totalPages > 1" class="flex gap-2 justify-between items-center text-xs pt-2 px-1">
          <button class="btn py-1 px-3 text-xs" :disabled="loading || page <= 1" @click="load(page - 1)">
            ← Ранее
          </button>
          <span class="text-slate-400 font-medium">
            Стр. {{ page }} из {{ totalPages }}
          </span>
          <button class="btn py-1 px-3 text-xs" :disabled="loading || !next" @click="load(page + 1)">
            Далее →
          </button>
        </nav>
      </aside>

      <!-- ПРАВАЯ КОЛОНКА: ДЕТАЛЬНЫЙ АНАЛИЗ ДИАЛОГА (7 колонок на lg) -->
      <main class="lg:col-span-7">
        <template v-if="detail">
          <article class="panel space-y-4">
            <!-- Шапка темы -->
            <div class="flex flex-wrap items-start justify-between gap-3 pb-3 border-b border-slate-800">
              <div class="space-y-1 min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-lg">💬</span>
                  <h3 class="font-bold text-base text-white break-words">
                    Тема #{{ detail.id }}: {{ detail.topic }}
                  </h3>
                </div>
                <div class="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  <span
                    class="px-2 py-0.5 rounded font-medium text-[11px]"
                    :class="detail.state === 'open' ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40' : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'"
                  >
                    {{ labels[detail.state] }} · Версия {{ detail.version }}
                  </span>
                  <span v-if="detail.company_name" class="px-2 py-0.5 rounded bg-indigo-950/50 text-indigo-300 border border-indigo-700/40">
                    🏢 {{ detail.company_name }}
                  </span>
                  <span v-if="detail.project_name" class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                    💼 {{ detail.project_name }}
                  </span>
                  <span v-if="detail.chat_name" class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                    📱 {{ detail.chat_name }}
                  </span>
                </div>
              </div>
              <button
                type="button"
                class="btn py-1.5 px-3 text-xs shrink-0 flex items-center gap-1.5 transition-all"
                :class="detail.is_subscribed ? 'bg-emerald-600/20 border-emerald-500 text-emerald-300 hover:bg-emerald-600/30' : 'bg-slate-800/80 border-slate-600 text-slate-200 hover:bg-slate-700'"
                :disabled="subscribing"
                @click="toggleSubscription"
                :title="detail.is_subscribed ? 'Отписаться от уведомлений по этой теме' : 'Подписаться на уведомления при изменении статуса этой темы'"
              >
                <span>{{ detail.is_subscribed ? '🔕' : '🔔' }}</span>
                <span>{{ detail.is_subscribed ? 'Вы подписаны' : 'Подписаться на тему' }}</span>
              </button>
            </div>

            <!-- Краткая суть диалога -->
            <div v-if="detail.summary" class="p-3 rounded-xl bg-slate-900/80 border border-slate-800 text-sm space-y-1">
              <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block">Краткая суть:</span>
              <p class="text-slate-200 whitespace-pre-wrap">{{ detail.summary }}</p>
            </div>

            <!-- Дочерние поддиалоги -->
            <section v-if="detail.children?.length" class="space-y-2">
              <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider">
                Вложенные поддиалоги ({{ detail.children.length }}):
              </h4>
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div
                  v-for="child in detail.children"
                  :key="child.id"
                  class="p-2.5 rounded-lg border border-slate-800 bg-slate-900/70 hover:border-indigo-500/60 cursor-pointer transition-all space-y-1"
                  @click="open(child.id)"
                >
                  <div class="flex items-center justify-between gap-1 text-xs">
                    <strong class="text-slate-200 truncate">#{{ child.id }}: {{ child.topic }}</strong>
                    <span
                      class="text-[10px] px-1.5 py-0.5 rounded"
                      :class="child.state === 'open' ? 'bg-amber-950/60 text-amber-300' : 'bg-emerald-950/60 text-emerald-300'"
                    >
                      {{ child.state }}
                    </span>
                  </div>
                  <p class="text-[11px] text-indigo-400 hover:underline">Перейти к поддиалогу →</p>
                </div>
              </div>
            </section>

            <!-- Выявленные факты и обязательства из этого диалога -->
            <section v-if="detail.facts?.length" class="space-y-2.5">
              <h4 class="text-xs font-bold text-indigo-300 uppercase tracking-wider flex items-center gap-1.5">
                <span>🎯</span> Выявленные обязательства и факты:
              </h4>
              <div class="space-y-2">
                <article
                  v-for="fact in detail.facts"
                  :key="fact.id"
                  class="p-3 rounded-xl border space-y-1.5 text-xs transition-colors"
                  :class="fact.in_progress ? 'bg-amber-950/20 border-amber-500/30' : 'bg-slate-900/90 border-slate-700/80'"
                >
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex items-center gap-2">
                      <span
                        class="px-2 py-0.5 rounded text-[10px] font-bold uppercase"
                        :class="fact.fact_type === 'commitment' ? 'bg-indigo-500/20 text-indigo-300' : fact.fact_type === 'payment' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-sky-500/20 text-sky-300'"
                      >
                        {{ factTypeLabels[fact.fact_type] || fact.fact_type }}
                      </span>
                      <span
                        v-if="fact.in_progress"
                        class="px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 text-[10px]"
                      >
                        ⏳ В процессе выявления
                      </span>
                    </div>
                    <div v-if="fact.proposed_changes?.amount" class="text-right">
                      <span class="text-sm font-bold text-emerald-400">
                        ₸ {{ formatMoney(fact.proposed_changes.amount) }}
                      </span>
                    </div>
                  </div>

                  <p v-if="fact.proposed_changes?.description" class="text-slate-200">
                    {{ fact.proposed_changes.description }}
                  </p>
                  <p v-else-if="fact.proposed_changes?.title" class="text-slate-200">
                    {{ fact.proposed_changes.title }}
                  </p>

                  <div v-if="fact.proposed_changes?.due_date" class="text-slate-400 text-[11px]">
                    Срок: <strong class="text-slate-300">{{ fact.proposed_changes.due_date }}</strong>
                    <span v-if="fact.proposed_changes?.party"> · Контрагент: {{ fact.proposed_changes.party }}</span>
                  </div>
                </article>
              </div>
            </section>

            <!-- Хронологическая переписка WhatsApp -->
            <section class="space-y-2">
              <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider">
                Сообщения диалога ({{ detail.messages.length }}):
              </h4>
              <div class="space-y-2.5 max-h-[500px] overflow-y-auto pr-1">
                <article
                  v-for="message in detail.messages"
                  :key="message.id"
                  class="p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-xs space-y-1.5"
                >
                  <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-1 text-[11px]">
                    <span class="font-semibold text-indigo-300">{{ message.sender_name }}</span>
                    <div class="flex items-center gap-1.5">
                      <span
                        class="px-1.5 py-0.2 rounded text-[10px]"
                        :class="message.thought_state === 'final' ? 'bg-emerald-950/60 text-emerald-300' : 'bg-slate-800 text-slate-400'"
                      >
                        {{ states[message.thought_state] }}
                      </span>
                      <span
                        v-if="message.relation"
                        class="px-1.5 py-0.2 rounded bg-indigo-950/60 text-indigo-300 text-[10px]"
                      >
                        {{ relationLabels[message.relation] || message.relation }}
                      </span>
                      <span v-if="message.sent_at || message.received_at" class="text-slate-500">
                        {{ new Date(message.sent_at || message.received_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}
                      </span>
                    </div>
                  </div>

                  <p class="text-slate-200 whitespace-pre-wrap leading-relaxed">{{ message.content }}</p>

                  <p v-if="message.rationale" class="text-[11px] text-slate-400 italic pt-0.5">
                    Обоснование: {{ message.rationale }}
                  </p>
                </article>
              </div>
            </section>
          </article>
        </template>

        <!-- Пустое состояние детальной панели -->
        <div v-else class="panel text-center py-12 text-slate-400 space-y-2">
          <span class="text-3xl block">🔍</span>
          <p class="text-sm font-medium text-slate-300">Выберите диалог или подтему в дереве слева</p>
          <p class="text-xs text-slate-500 max-w-sm mx-auto">
            Здесь отобразятся детализированные реплики участников, выявленные финансовые обязательства и поддиалоги.
          </p>
        </div>
      </main>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, post } from '../../composables/api'
import { currentUser } from '../../composables/session'
import type { Page, Directory } from '../../types/platform'
import type { DialogueThread } from '../../types/factReview'

type ThreadSummary = Omit<DialogueThread, 'messages'>

const chats = ref<NonNullable<Directory['chats']>>([])
const chatId = ref<number | null>(null)
const notice = ref('')
const lead = computed(() => currentUser.value?.roles.includes('team_lead'))

const search = ref('')
const statusFilter = ref<'all' | 'open' | 'ready'>('all')
const page = ref(1)
const totalCount = ref(0)
const totalPages = computed(() => Math.max(1, Math.ceil(totalCount.value / 10)))
const next = ref(false)
const prev = ref(false)
const loading = ref(false)
const error = ref('')

const serverStats = ref<{ total: number; open: number; ready: number; commitments?: number }>({
  total: 0,
  open: 0,
  ready: 0,
  commitments: 0,
})
const subscribing = ref(false)

const items = ref<ThreadSummary[]>([])
const detail = ref<DialogueThread | null>(null)
const closedFolders = ref<Set<string>>(new Set())

const labels: Record<string, string> = {
  open: 'Мысль продолжается',
  ready: 'Мысль определена',
  unknown: 'Требует уточнения',
  superseded: 'Заменена',
}

const states = {
  intermediate: 'Промежуточная реплика',
  final: 'Завершает мысль',
  unknown: 'Связь неизвестна',
}

const relationLabels: Record<string, string> = {
  discusses: 'Обсуждение',
  answers: 'Ответ',
  clarifies: 'Уточнение',
  cancels: 'Отмена',
  fulfills: 'Исполнение',
}

const factTypeLabels: Record<string, string> = {
  commitment: 'Обязательство',
  payment: 'Платеж',
  project: 'Сделка / Проект',
}

function formatMoney(amount: number | string | null | undefined): string {
  if (amount == null) return '0'
  const num = typeof amount === 'string' ? parseFloat(amount) : amount
  if (isNaN(num)) return String(amount)
  return num.toLocaleString('ru-RU')
}

// Статистика по загруженным тредам
const stats = computed(() => {
  return serverStats.value
})

// Поддиалоги по ID родителя
const subthreadsByParent = computed(() => {
  const map: Record<number, ThreadSummary[]> = {}
  for (const item of items.value) {
    if (item.parent_id) {
      if (!map[item.parent_id]) map[item.parent_id] = []
      map[item.parent_id].push(item)
    }
  }
  return map
})

// Иерархическое дерево: Компании -> Сделки -> Корневые треды
interface TreeProject {
  key: string
  name: string
  threads: ThreadSummary[]
}

interface TreeCompany {
  key: string
  name: string
  projects: TreeProject[]
  threadCount: number
}

const treeCompanies = computed(() => {
  const companyMap = new Map<string, { name: string; projectsMap: Map<string, ThreadSummary[]>; count: number }>()

  // Итерируем по тредам
  for (const thread of items.value) {
    let compName = thread.company_name?.trim() || thread.counterparty?.trim()
    if (!compName && thread.chat_name) {
      compName = `Чат: ${thread.chat_name}`
    }
    if (!compName) {
      compName = 'Общие диалоги'
    }

    let projName = thread.project_name?.trim()
    if (!projName) {
      projName = 'Диалоги'
    }

    if (!companyMap.has(compName)) {
      companyMap.set(compName, { name: compName, projectsMap: new Map(), count: 0 })
    }
    const compEntry = companyMap.get(compName)!
    compEntry.count++

    if (!compEntry.projectsMap.has(projName)) {
      compEntry.projectsMap.set(projName, [])
    }

    // Если тред не является дочерним поддиалогом, добавляем его в список верхнего уровня сделки
    if (!thread.parent_id) {
      compEntry.projectsMap.get(projName)!.push(thread)
    } else {
      // Если родитель треда не найден в выборке, показываем его тоже
      const parentExists = items.value.some(it => it.id === thread.parent_id)
      if (!parentExists) {
        compEntry.projectsMap.get(projName)!.push(thread)
      }
    }
  }

  const result: TreeCompany[] = []
  for (const [compName, compData] of companyMap.entries()) {
    const projects: TreeProject[] = []
    for (const [projName, threads] of compData.projectsMap.entries()) {
      if (threads.length > 0) {
        projects.push({
          key: `proj-${compName}-${projName}`,
          name: projName,
          threads,
        })
      }
    }
    if (projects.length > 0) {
      result.push({
        key: `comp-${compName}`,
        name: compName,
        projects,
        threadCount: compData.count,
      })
    }
  }
  return result
})

function isFolderOpen(key: string): boolean {
  return !closedFolders.value.has(key)
}

function toggleFolder(key: string) {
  if (closedFolders.value.has(key)) {
    closedFolders.value.delete(key)
  } else {
    closedFolders.value.add(key)
  }
}

function toggleAllFolders(expand: boolean) {
  if (expand) {
    closedFolders.value.clear()
    return
  }
  for (const comp of treeCompanies.value) {
    closedFolders.value.add(comp.key)
    for (const proj of comp.projects) {
      closedFolders.value.add(proj.key)
    }
  }
}

function setStatusFilter(filter: 'all' | 'open' | 'ready') {
  statusFilter.value = filter
  load(1)
}

async function load(number: number) {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams()
    params.set('page', String(number))
    if (search.value.trim()) {
      params.set('search', search.value.trim())
    }
    if (statusFilter.value !== 'all') {
      params.set('state', statusFilter.value)
    }

    const result = await api<Page<ThreadSummary>>(`/threads/?${params.toString()}`)
    items.value = result.results
    totalCount.value = result.count
    next.value = Boolean(result.next)
    prev.value = Boolean(result.previous)
    page.value = number
    if (result.stats) {
      serverStats.value = result.stats
    }

    // Если был выбран тред, проверяем остался ли он в списке
    if (detail.value && !items.value.some(it => it.id === detail.value!.id)) {
      detail.value = null
    } else if (!detail.value && items.value.length > 0) {
      // Автоматически открываем первый тред
      open(items.value[0].id)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Не удалось загрузить темы'
  } finally {
    loading.value = false
  }
}

async function open(id: number) {
  try {
    detail.value = await api<DialogueThread>(`/threads/${id}/`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Тема недоступна'
  }
}

async function toggleSubscription() {
  if (!detail.value) return
  subscribing.value = true
  try {
    const res = await post<{ subscribed: boolean; thread_id: number }>(`/threads/${detail.value.id}/subscribe/`, {})
    detail.value.is_subscribed = res.subscribed
    const item = items.value.find(it => it.id === detail.value!.id)
    if (item) {
      item.is_subscribed = res.subscribed
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Не удалось изменить подписку'
  } finally {
    subscribing.value = false
  }
}

async function rebuild() {
  if (!chatId.value) return
  try {
    await post('/threads/backfill/', { config_id: chatId.value, request_key: crypto.randomUUID() })
    notice.value = 'Разбор истории поставлен в очередь. Обновите список тем после обработки.'
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Не удалось запустить разбор'
  }
}

onMounted(async () => {
  await load(1)
  try {
    const directory = await api<Directory>('/directory/')
    chats.value = directory.chats || []
    chatId.value = chats.value[0]?.id || null
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Не удалось загрузить чаты'
  }
})
</script>
