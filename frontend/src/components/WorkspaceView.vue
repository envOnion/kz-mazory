<template>
<section class="max-w-6xl mx-auto w-full p-4 md:p-6 space-y-5">
  <header><p class="text-xs text-indigo-300">{{ client ? 'КАБИНЕТ КЛИЕНТА' : 'РАБОЧИЙ КАБИНЕТ' }}</p><h1 class="text-2xl font-semibold">{{ currentUser?.name }}</h1><p class="text-sm text-slate-400">{{ currentUser?.roles.map(role => roleLabels[role]).join(' · ') }}</p></header>
  <nav class="flex gap-2 flex-wrap" aria-label="Разделы кабинета"><button v-for="item in tabs" :key="item.id" class="btn" :class="tab === item.id ? 'bg-indigo-600 border-indigo-400' : ''" @click="selectTab(item.id)">{{ item.label }}</button></nav>
  <p v-if="error" role="alert" class="panel text-rose-300">{{ error }}</p><p v-if="notice" role="status" class="text-emerald-300">{{ notice }}</p><p v-if="loading" role="status">Загрузка…</p>
  <template v-if="tab === 'people'">
    <header class="space-y-2"><h2 class="text-xl font-semibold">Сотрудники и участники WhatsApp</h2><p class="text-sm text-slate-400">Телефон связывает участника с учётной записью. Доступ к кабинету предоставляется отдельно.</p></header>
    <div class="flex gap-2 flex-wrap"><button class="btn" :disabled="busy || loading" @click="load">Обновить список</button><button v-for="chat in (lead ? directory.chats || [] : [])" :key="chat.id" class="btn" :disabled="busy" @click="syncParticipants(chat.id)">Восстановить участников · {{ chat.name }}</button></div>
    <p v-for="sync in directory.participant_sync || []" :key="sync.config_id" class="text-sm text-slate-400">{{ sync.chat_name }} · {{ participantSyncLabel(sync.state) }}<span v-if="sync.summary"> · Последняя загрузка: {{ new Date(sync.summary.synced_at).toLocaleString('ru-RU') }}</span><span v-if="sync.error_code" class="text-amber-300"> · Не удалось завершить восстановление. Обнаруженные участники сохранены; повторите загрузку.</span></p>
    <p class="text-sm text-slate-400">Участников: {{ directory.participants?.length || 0 }} · С телефоном: {{ directory.participants?.filter(person => person.phone).length || 0 }}</p>
    <div class="grid md:grid-cols-2 gap-4"><article v-for="person in directory.participants || []" :key="person.id" class="panel space-y-2" :data-testid="`participant-${person.id}`"><h3 class="font-semibold">{{ person.display_name }}</h3><p class="text-sm text-slate-400">{{ person.team_name }}</p><p>{{ person.phone ? `+${person.phone}` : 'Телефон не установлен' }}</p><p v-if="person.resolution_state === 'conflict'" class="text-amber-300">Данные идентификации противоречат друг другу — требуется проверка.</p><p v-else-if="!person.phone" class="text-sm text-slate-400">В источнике есть имя или WhatsApp ID; подтверждённый номер пока не получен.</p><p v-if="person.user_id" class="text-sm">Учётная запись создана · {{ person.access_status === 'active' ? 'Доступ предоставлен' : 'Доступ не предоставлен' }}</p><details v-if="person.aliases.length > 1" class="text-sm text-slate-400"><summary>Имена в переписке</summary>{{ person.aliases.join(' · ') }}</details></article></div>
    <p v-if="!directory.participants?.length && !loading" class="panel">Участники ещё не загружены. Восстановите их из настроенного чата.</p>
  </template>
  <template v-if="tab === 'projects'">
    <template v-if="!client">
      <nav class="flex gap-2 flex-wrap" aria-label="Данные проектов"><button v-for="group in projectGroups" :key="group.id" class="btn" :class="projectGroup === group.id ? 'bg-indigo-600 border-indigo-400' : ''" :disabled="loading" @click="projectGroup = group.id; filterProjects()">{{ group.label }} · {{ projectCounts[group.id] }}</button></nav>
      <form class="panel grid sm:grid-cols-2 lg:grid-cols-3 gap-3" @submit.prevent="filterProjects">
        <label>Проект или компания<input v-model="projectSearch" class="field w-full" placeholder="Название" /></label>
        <label>Стадия<select v-model="projectStage" class="field w-full" @change="filterProjects"><option value="">Все стадии</option><option v-for="stage in projectStages" :key="stage.id" :value="stage.id">{{ stage.label }}</option></select></label>
        <label>Полнота данных<select v-model="projectCompleteness" class="field w-full" @change="filterProjects"><option value="all">Все</option><option value="complete">Полные</option><option value="partial">Частичные</option><option value="missing">Нет финансовых данных</option></select></label>
        <label>Команда<select v-model="projectTeam" class="field w-full" @change="filterProjects"><option :value="null">Все доступные</option><option v-for="team in directory.teams" :key="team.id" :value="team.id">{{ team.name }}</option></select></label>
        <label>Ответственный<select v-model="projectManager" class="field w-full" @change="filterProjects"><option :value="null">Все доступные</option><option v-for="profile in directory.profiles" :key="profile.id" :value="profile.id">{{ profile.full_name }}</option></select></label>
        <button class="btn-primary self-end" :disabled="loading" type="submit">Найти</button>
      </form>
      <p class="text-sm text-slate-400">По фильтрам: {{ projectCount }}. Данные подтверждаются по переписке WhatsApp.</p>
    </template>
    <div class="grid md:grid-cols-2 gap-4"><article v-for="project in projects" :key="project.id" class="panel space-y-2"><h2 class="font-semibold">{{ project.name }}</h2><p>{{ project.status }} · Версия {{ project.version }}</p><template v-if="!client"><p>Договор: {{ project.contract_known ? project.contract_formatted : 'Сумма не указана' }}</p><p>Поступило: {{ project.payments_known ? project.paid_formatted : 'Точная сумма не установлена' }}</p><p v-if="project.approximate_paid_formatted">Приблизительное поступление: {{ project.approximate_paid_formatted }}</p><p>Остаток: {{ project.balance_known ? project.due_formatted : 'Не определён' }}</p><section v-if="project.missing_data_reasons?.length" class="rounded-lg bg-slate-800/60 p-3 text-sm space-y-1" aria-label="Причины неполных данных"><p v-for="(reason, index) in project.missing_data_reasons" :key="index">{{ reason.message }}</p><button v-if="project.review_available" class="btn" @click="showProjectFacts(project)">Проверить связанные факты</button></section><button class="btn" @click="showHistory(project.id)">История подтверждений</button><details v-if="lead && !automatic" class="pt-2"><summary>Сменить ответственного</summary><select class="field" v-model="assignments[project.id]"><option v-for="profile in directory.profiles" :key="profile.id" :value="profile.id">{{ profile.full_name }}</option></select><input class="field" v-model="reasons[project.id]" placeholder="Причина смены ответственного" /><label class="block"><input type="checkbox" v-model="transferTasks[project.id]" /> Передать открытые обязательства</label><button class="btn" :disabled="busy || !assignments[project.id] || !reasons[project.id]" @click="assignProject(project)">Сохранить ответственного</button></details></template></article></div><p v-if="!projects.length && !loading" class="panel">Нет проектов по выбранным фильтрам. Измените список или условия поиска.</p>
    <nav v-if="!client" class="flex gap-3 items-center" aria-label="Страницы проектов"><button class="btn" :disabled="loading || projectPage === 1" @click="changeProjectPage(-1)">Предыдущая</button><span>Страница {{ projectPage }}</span><button class="btn" :disabled="loading || !projectNext" @click="changeProjectPage(1)">Следующая</button></nav>
  </template>
  <template v-if="tab === 'review'">
    <p v-if="candidateProject" class="text-sm">Факты проекта: {{ candidateProjectName }} <button class="btn" @click="candidateProject = null; candidatePage = 1; load()">Показать все факты</button></p>
    <p class="text-sm text-slate-400">{{ automatic ? 'Система принимает решения по переписке WhatsApp автоматически. Здесь показаны основания и причины ожидания данных.' : 'AI предлагает изменения. Утверждённые суммы остаются прежними до подтверждения. Платежи проверяет финансист.' }}</p>
    <label>Статус <select class="field" v-model="candidateStatus" @change="candidatePage = 1; load()"><option value="pending">На проверке</option><option value="approved">Принято</option><option value="rejected">Отклонено</option><option value="superseded">Заменено</option></select></label>
    <label class="ml-3">Тип <select class="field" v-model="candidateFactType" @change="candidatePage = 1; load()"><option value="">Все факты</option><option value="project">Проекты — проверить и создать</option><option value="commitment">Обязательства</option><option value="payment">Платежи</option></select></label>
    <section class="panel space-y-2" aria-label="Справочник проектов CRM">
      <p v-if="directory.crm_connection" class="text-sm">Подключение Bitrix: {{ directory.crm_connection.configured ? 'настроено' : 'не настроено' }} · Поиск сделок: {{ directory.crm_connection.matching_enabled ? 'включён' : 'выключен' }}</p>
      <p v-for="state in directory.crm_catalog || []" :key="state.team_id" :class="state.state === 'error' ? 'text-rose-300' : 'text-slate-300'">
        {{ catalogLabel(state.state) }} · Загружено: {{ state.imported_count }}
        <span v-if="state.last_success_at"> · Последняя загрузка: {{ new Date(state.last_success_at).toLocaleString('ru-RU') }}</span>
        <span v-if="state.error_code"> · {{ catalogError(state.error_code) }}</span>
        <button v-if="lead" class="btn ml-2" :disabled="busy || ['queued', 'running'].includes(state.state)" @click="syncCatalog(state.team_id)">Загрузить из CRM</button>
      </p>
      <p v-if="!directory.projects.length">Доступных проектов пока нет. Справочник загружается из Bitrix CRM; общие обязательства команды можно подтверждать без проекта.</p>
      <p v-else>Проекты из CRM доступны для привязки. Финансовые данные подтверждаются отдельно.</p>
      <button class="btn" :disabled="busy || loading" @click="refreshDirectory">Обновить справочник</button>
      <button v-for="team in (lead || financeRole ? directory.teams : [])" :key="team.id" class="btn ml-2" :disabled="busy || !directory.crm_connection?.matching_enabled" @click="retryCrm(team.id)">Повторить поиск CRM · {{ team.name }}</button>
    </section>
    <p class="text-sm text-slate-400">Предложений: {{ candidateCount }}. Связанные факты на этой странице собраны по проекту или чату.</p>
    <template v-for="group in candidateGroups" :key="group.key">
    <h2 class="text-lg font-semibold pt-3">{{ group.label }} <span class="text-sm text-slate-400">· {{ group.items.length }} на этой странице</span></h2>
    <article v-for="item in group.items" :key="item.id" class="panel space-y-4" :data-testid="`candidate-${item.id}`">
      <p v-if="item.thread" class="text-sm text-indigo-300">Тема #{{ item.thread.id }}: {{ item.thread.topic }} · Версия {{ item.thread.version }}</p>
      <FactSummary :item="item" />
      <p v-if="item.automatic_decision" class="rounded bg-slate-800 p-3 text-sm">Автоматическое решение: {{ item.automatic_decision.explanation }}</p>
      <section class="space-y-2"><h3 class="text-sm font-semibold">Чем подтверждается факт</h3>
      <blockquote v-for="evidence in item.evidence" :key="evidence.id" class="border-l-2 border-indigo-400 pl-3 text-sm whitespace-pre-wrap"><strong v-if="evidence.role" class="block text-xs text-indigo-200">{{ ({request: 'Просьба / предмет задачи', promise: 'Обещание / назначение', deadline: 'Срок', fulfillment: 'Доказательство выполнения'} as Record<string, string>)[evidence.role] || 'Первоисточник' }}</strong>{{ evidence.quote }}</blockquote>
      <p v-if="!item.evidence.length" class="text-amber-300 text-sm">Первоисточник недоступен. Подтверждение невозможно без доступа к нему.</p></section>
      <FactConversation :candidate-id="item.id" />
      <label v-if="item.status === 'pending' && (canReview(item) || canApprove(item) || canSelectCrm(item))" class="block text-sm">Основание / причина<input class="field w-full mt-1" v-model="reasons[item.id]" /></label>
      <section class="rounded-xl border border-sky-700/60 bg-sky-950/20 p-3 space-y-3" :data-crm-state="item.crm_resolution.state">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 class="font-semibold text-sky-200">Сопоставление с CRM</h3>
            <p class="text-sm" :class="crmStateTone(item.crm_resolution.state)">{{ crmStateLabel(item) }}</p>
          </div>
          <span class="text-xs text-slate-400">Проверка №{{ item.crm_resolution.revision }}<template v-if="item.crm_resolution.checked_at"> · {{ new Date(item.crm_resolution.checked_at).toLocaleString() }}</template></span>
        </div>
        <p v-if="item.crm_resolution.error_code" class="text-sm text-rose-300">{{ crmErrors[item.crm_resolution.error_code] || 'Не удалось проверить сделку. Повторите обработку позже или обратитесь к администратору.' }}</p>
        <p v-if="item.crm_resolution.not_requested_reason" class="text-sm text-slate-400">{{ item.crm_resolution.not_requested_reason }}</p>
        <button v-if="item.status === 'pending' && (lead || financeRole) && ['disabled', 'error', 'not_requested'].includes(item.crm_resolution.state)" class="btn" :disabled="busy || !directory.crm_connection?.matching_enabled || Boolean(item.crm_resolution.not_requested_reason)" @click="retryCrm(item.team_id, item.id)">Повторить поиск сделки</button>
        <details v-if="item.crm_resolution.error_code" class="text-xs text-slate-500"><summary>Сведения для администратора</summary>{{ item.crm_resolution.error_code }}</details>
        <fieldset v-if="item.crm_resolution.options.length" class="space-y-2">
          <legend class="text-xs text-slate-400 mb-2">Варианты отсортированы по релевантности; оценка сама по себе не создаёт связь.</legend>
          <label v-for="option in item.crm_resolution.options" :key="option.id" class="flex items-start gap-3 rounded-lg border border-slate-700 p-3 cursor-pointer hover:border-sky-500">
            <input v-if="item.status === 'pending' && canSelectCrm(item)" v-model="crmSelections[item.id]" type="radio" :name="`crm-match-${item.id}`" :value="option.id" class="mt-1" />
            <span class="min-w-0 flex-1 space-y-1">
              <span class="flex flex-wrap justify-between gap-2"><strong class="break-words">#{{ option.bitrix_deal_id }} · {{ option.deal_title || 'Без названия' }}</strong><span class="text-xs text-slate-400">Рейтинг {{ option.score }}/100</span></span>
              <span class="block text-sm text-slate-300">{{ option.company_name || 'Компания не указана' }}<template v-if="option.object_label"> · Объект: {{ option.object_label }}</template></span>
              <span class="block text-xs text-slate-400">{{ option.stage_label || 'Стадия сделки не расшифрована' }} · Сумма сделки: {{ option.opportunity ?? 'Не указана' }} {{ option.currency }}</span>
              <span v-if="option.match_reasons.length" class="block text-xs text-sky-300">{{ option.match_reasons.map(reason => crmReasons[reason] || 'Совпадают признаки сделки').join(' · ') }}</span>
              <span v-if="option.selection_state === 'selected'" class="inline-block text-xs font-semibold text-emerald-300">Выбрано</span>
            </span>
          </label>
        </fieldset>
        <div v-if="item.status === 'pending' && canSelectCrm(item) && item.crm_resolution.options.length && ['ambiguous', 'matched'].includes(item.crm_resolution.state)" class="flex flex-wrap items-end gap-2">
          <button class="btn" :disabled="busy || !crmSelections[item.id] || !reasons[item.id]?.trim()" @click="matchCrm(item)">Сопоставить с CRM</button>
          <span class="text-xs text-slate-400">Выбор сохраняется отдельно и не подтверждает AI-факт.</span>
        </div>
      </section>
      <p v-if="item.status === 'pending' && approvalHint(item)" class="text-sm text-amber-300">{{ approvalHint(item) }}</p>
      <template v-if="item.status === 'pending' && (canReview(item) || canApprove(item))">
        <p v-if="requiresFinanceForCrmMaterialization(item) && !financeRole" class="text-sm text-amber-300">Выбрать CRM-сделку или отклонить предложение можно сейчас, но сумму из CRM должен подтвердить пользователь с ролью финансиста.</p>
        <div v-if="editing !== item.id" class="flex flex-wrap gap-2"><button class="btn-primary" :disabled="busy || Boolean(approvalHint(item))" @click="reviewItem(item, 'approve')">Подтвердить факт</button><button v-if="canReview(item)" class="btn" :disabled="busy" @click="editing = item.id">Исправить значения</button><button v-if="canReview(item)" class="btn" :disabled="busy || !reasons[item.id]?.trim()" @click="reviewItem(item, 'reject')">Отклонить</button></div>
        <p v-if="!reasons[item.id]?.trim() && editing !== item.id" class="text-xs text-slate-400">Для отклонения или сопоставления укажите причину в поле выше.</p>
        <details v-if="canReview(item) && directory.projects.some(p => p.team_id === item.team_id)"><summary class="text-sm text-slate-400 cursor-pointer">Выбрать существующий проект / обновить данные перед проверкой</summary><div class="flex gap-2 flex-wrap items-center mt-2"><select class="field max-w-xs w-full truncate" v-model="matches[item.id]" aria-label="Сопоставить с проектом"><option :value="undefined">Выберите проект</option><option v-for="p in directory.projects.filter(p => p.team_id === item.team_id)" :key="p.id" :value="p.id">{{ p.name }}</option></select><button class="btn" :disabled="busy || !matches[item.id] || !reasons[item.id]?.trim()" @click="matchItem(item)">Связать с проектом и обновить</button></div></details>
        <FactCorrectionForm v-if="canReview(item) && editing === item.id" :key="item.id" :item="item" :busy="busy" :initial-reason="reasons[item.id] || ''" :can-approve="Boolean(item.evidence.length && item.source_available !== false && canApprove(item))" @cancel="editing = null" @save="(changes, reason) => saveCorrection(item, changes, reason)" />
      </template><p v-else class="text-sm text-slate-400">{{ statusLabels[item.status] }}<template v-if="item.review_reason"> · {{ item.review_reason }}</template></p>
    </article></template>
    <nav class="flex gap-3 items-center" aria-label="Страницы предложений"><button class="btn" :disabled="loading || busy || candidatePage === 1" @click="changeCandidatePage(-1)">Предыдущая</button><span class="text-sm">Страница {{ candidatePage }}</span><button class="btn" :disabled="loading || busy || !candidateNext" @click="changeCandidatePage(1)">Следующая</button></nav><p v-if="!candidates.length && !loading" class="panel">Предложений с этим статусом нет.</p>
  </template>
  <AutonomousReports v-if="tab === 'reports'" />
  <ThreadBrowser v-if="tab === 'threads'" />
  <template v-if="tab === 'tasks'">
    <p class="text-sm text-slate-400">Сроки указаны в UTC+6. Чтение уведомления не закрывает обязательство. Перенос сохраняет исходный срок.</p>
    <article v-for="item in commitments?.commitments" :key="item.id" class="panel space-y-3"><h2 class="font-semibold">{{ item.project_name }} · {{ item.manager_name }}</h2><p>{{ item.text }}</p><p :class="item.status_color === 'red' ? 'text-rose-300' : 'text-slate-400'">{{ item.deadline_formatted }} · {{ item.status }}</p><p v-if="item.postponed_reason">Причина переноса: {{ item.postponed_reason }}</p><div v-if="!automatic && ['pending','overdue'].includes(item.status_code)" class="flex flex-wrap gap-2"><button class="btn-primary" :disabled="busy" @click="taskAction(item, 'fulfill')">Выполнено</button><button class="btn" :disabled="busy" @click="taskAction(item, 'help')">Нужна помощь</button><button v-if="lead" class="btn" :disabled="busy" @click="taskAction(item, 'postpone')">Перенести</button><input type="datetime-local" class="field" v-if="lead" v-model="deadlines[item.id]" aria-label="Новый срок" /><input class="field flex-1" v-model="reasons[item.id]" placeholder="Причина переноса или запрос помощи" /></div></article>
    <p v-if="commitments && !commitments.total_count" class="panel">Подтверждённых обязательств пока нет.</p>
  </template>
  <template v-if="tab === 'notifications'">
    <button class="btn" @click="markAllAsRead">Прочитать все</button><article v-for="item in notifications" :key="item.id" class="panel space-y-2"><h2 class="font-semibold">{{ item.title }}</h2><p>{{ item.message }}</p><p class="text-xs text-slate-400">{{ new Date(item.created_at).toLocaleString() }} · {{ item.is_read ? 'Прочитано' : 'Новое' }}</p><p v-for="(delivery, index) in item.deliveries" :key="index" class="text-xs">WhatsApp: {{ deliveryLabel(delivery.state) }}</p><button v-if="!item.acknowledged" class="btn" @click="acknowledge(item.id)">Подтвердить получение</button></article>
    <form v-if="lead || financeRole" @submit.prevent="sendNotice" class="panel space-y-3"><h2 class="font-semibold">Адресное уведомление</h2><label class="block max-w-sm">Получатель<select class="field block w-full truncate mt-1" v-model="noticeForm.recipient_id" required><option v-for="p in directory.profiles" :key="p.id" :value="p.user_id">{{ p.full_name }}</option></select></label><input class="field w-full" v-model="noticeForm.title" placeholder="Заголовок" required maxlength="255" /><textarea class="field w-full" v-model="noticeForm.message" placeholder="Сообщение" required maxlength="4000" /><label class="block"><input type="checkbox" v-model="noticeForm.send_whatsapp" /> Продублировать в WhatsApp</label><button class="btn-primary" :disabled="busy">Отправить уведомление</button></form>
  </template>
  <dialog ref="dialog" class="w-[min(90vw,800px)] rounded-2xl bg-slate-900 text-slate-100 border border-slate-600 p-6 backdrop:bg-black/70"><div class="flex justify-between gap-3 mb-4"><h2 class="font-semibold">{{ modalTitle }}</h2><button class="btn" @click="dialog?.close()">Закрыть</button></div><pre class="whitespace-pre-wrap break-words text-sm max-h-[65vh] overflow-auto">{{ modalText }}</pre></dialog>
</section>
</template>
<script setup lang="ts">
import AutonomousReports from './AutonomousReports.vue'
import ThreadBrowser from './review/ThreadBrowser.vue'
import { ref, computed, onMounted } from 'vue'
import FactSummary from './review/FactSummary.vue'
import FactConversation from './review/FactConversation.vue'
import FactCorrectionForm from './review/FactCorrectionForm.vue'
import { roles as roleLabels, statusLabels, crmErrors, crmReasons } from './review/presentation'
import { api, post } from '../composables/api'
import { currentUser } from '../composables/session'
import { useNotifications } from '../composables/useNotifications'
import type { Page, Candidate, CrmMatchState, Directory } from '../types/platform'
import type { ManagerProjectSummary, ProjectWorkspaceSummary, CommitmentData, CommitmentItem } from '../types/chat'
const roles = computed(() => currentUser.value?.roles || [])
const client = computed(() => roles.value.includes('client')), lead = computed(() => roles.value.includes('team_lead')), financeRole = computed(() => roles.value.includes('finance'))
const tab = ref('projects'), loading = ref(false), busy = ref(false), error = ref(''), notice = ref('')
const tabs = computed(() => [
  { id: 'projects', label: 'Проекты' },
  ...(lead.value || financeRole.value ? [{ id: 'people', label: 'Сотрудники' }] : []),
  ...(!client.value ? [{ id: 'reports', label: 'Отчеты по WhatsApp' }] : []),
  ...(!client.value ? [
    { id: 'review', label: automatic.value ? 'Решения системы' : 'Проверка фактов' },
    { id: 'threads', label: 'Темы переписки' },
    { id: 'tasks', label: 'Обязательства' }
  ] : []),
  { id: 'notifications', label: 'Уведомления' }
])
const assignments = ref<Record<number, number>>({}), transferTasks = ref<Record<number, boolean>>({})
const projects = ref<ProjectWorkspaceSummary[]>([]), candidates = ref<Candidate[]>([]), commitments = ref<CommitmentData | null>(null)
const directory = ref<Directory>({ teams: [], profiles: [], projects: [] })
const automatic = computed(() => directory.value.autonomous_enabled === true)
const candidateProject = ref<number | null>(null), candidateProjectName = ref('')
async function showProjectFacts(project: ProjectWorkspaceSummary) { candidateProject.value = project.id; candidateProjectName.value = project.name; candidatePage.value = 1; candidateStatus.value = 'pending'; await selectTab('review') }
const candidateFactType = ref(''), candidateStatus = ref('pending'), editing = ref<number | null>(null), reasons = ref<Record<number, string>>({}), deadlines = ref<Record<number, string>>({}), matches = ref<Record<number, number>>({})
const projectPage = ref(1), projectCount = ref(0), projectNext = ref(false)
const projectGroup = ref('with_data'), projectSearch = ref(''), projectStage = ref(''), projectCompleteness = ref('all')
const projectTeam = ref<number | null>(null), projectManager = ref<number | null>(null)
const projectCounts = ref<Record<string, number>>({ with_data: 0, without_data: 0, all: 0 })
const projectGroups = [{ id: 'with_data', label: 'С финансовыми данными' }, { id: 'without_data', label: 'Без финансовых данных' }, { id: 'all', label: 'Все проекты' }]
const projectStages = [{ id: 'lead', label: 'Первичный контакт' }, { id: 'qualification', label: 'Квалификация' }, { id: 'design', label: 'Проектирование' }, { id: 'proposal_sent', label: 'КП отправлено' }, { id: 'contract_signing', label: 'Согласование договора' }, { id: 'in_execution', label: 'В исполнении' }, { id: 'completed', label: 'Завершён' }, { id: 'stalled', label: 'Требует внимания' }, { id: 'lost', label: 'Проигран' }]
async function filterProjects() { projectPage.value = 1; await load() }
async function changeProjectPage(delta: number) { projectPage.value += delta; await load() }
const candidatePage = ref(1), candidateCount = ref(0), candidateNext = ref(false)
const candidateGroups = computed(() => {
  const groups = new Map<string, { key: string; label: string; items: Candidate[] }>()
  for (const item of candidates.value) {
    const key = item.project_id ? `project:${item.project_id}` : item.conversation_key ? `chat:${item.conversation_key}` : `fact:${item.id}`
    if (!groups.has(key)) groups.set(key, { key, label: item.project_name || item.chat_name || 'Предложение без выбранного проекта', items: [] })
    groups.get(key)!.items.push(item)
  }
  return [...groups.values()]
})
async function changeCandidatePage(delta: number) { candidatePage.value += delta; editing.value = null; await load() }
const crmSelections = ref<Record<number, number>>({})
const noticeForm = ref({ recipient_id: 0, title: '', message: '', send_whatsapp: false })
const dialog = ref<HTMLDialogElement>(), modalTitle = ref(''), modalText = ref('')
const { notifications, fetchNotifications, markAllAsRead, acknowledge, dispatchNotification } = useNotifications()
function canReview(item: Candidate) {
  if (automatic.value) return false
  const financialProject = item.fact_type === 'project' && ('contract_amount' in item.proposed_changes || 'cost_amount' in item.proposed_changes)
  return item.fact_type === 'payment' || financialProject ? financeRole.value : lead.value
}
function canSelectCrm(_item: Candidate) { return !automatic.value && lead.value }
function requiresFinanceForCrmMaterialization(item: Candidate) {
  const selected = item.crm_resolution.options.find(option => option.selection_state === 'selected')
  if (!selected) return false
  return item.crm_resolution.state === 'matched'
    && item.project_id === null
    && selected.project_id === null
    && selected.opportunity !== null
}
function canApprove(item: Candidate) { return !automatic.value && (requiresFinanceForCrmMaterialization(item) ? financeRole.value : canReview(item)) }
function approvalHint(item: Candidate) {
  if (automatic.value) return ''
  if (!item.evidence.length || item.source_available === false) return 'Для подтверждения нужен доступ к первоисточнику.'
  if (!canApprove(item)) return 'Для подтверждения финансовых данных требуется роль финансиста.'
  if (item.thread && item.fact_type === 'project' && !item.project_id && !['matched', 'not_found'].includes(item.crm_resolution.state)) return 'Сначала завершите поиск CRM и выберите существующий проект либо подтвердите отсутствие совпадений.'
  if (item.fact_type === 'payment' && !item.project_id) return 'Сначала выберите существующий подтверждённый проект.'
  if (item.project_id && item.base_version !== item.current_version) return 'Данные проекта изменились. Выберите проект и обновите данные перед проверкой.'
  if (item.fact_type === 'payment' && !['increment', 'reversal'].includes(String(item.proposed_changes.payment_kind || 'increment'))) return 'Обещание и накопленный итог нельзя принять как новый платёж. Исправляйте вид только при подтверждении в переписке, иначе отклоните предложение.'
  if (item.fact_type === 'payment' && !item.proposed_changes.payment_date) return 'Укажите подтверждённую дату платежа через форму исправления.'
  return ''
}
function deliveryLabel(state: string) { return ({ sent: 'Отправлено', delivered: 'Доставлено', unknown: 'Результат неизвестен; автоматический повтор остановлен', queued: 'В очереди', failed: 'Ошибка отправки', cancelled: 'Отменено' } as Record<string, string>)[state] || state }
function crmStateTone(state: CrmMatchState) { return ({ matched: 'text-emerald-300', ambiguous: 'text-amber-300', error: 'text-rose-300', queued: 'text-sky-300', not_found: 'text-slate-300', disabled: 'text-slate-300', not_requested: 'text-slate-300' } satisfies Record<CrmMatchState, string>)[state] }
function crmStateLabel(item: Candidate) {
  const options = item.crm_resolution.options.length
  return ({
    not_requested: 'CRM-сопоставление ещё не запускалось.',
    queued: 'CRM-сопоставление в очереди или выполняется.',
    matched: 'Связано с выбранной сделкой CRM.',
    ambiguous: options === 1 ? 'Найден один вариант — требуется подтверждение.' : `Найдено вариантов: ${options}. Требуется выбор.`,
    not_found: 'Поиск выполнен: совпадений в CRM не найдено.',
    disabled: 'Поиск не выполнялся: сопоставление с CRM отключено.',
    error: 'Поиск CRM завершился ошибкой; отсутствие сделки не установлено.',
  } satisfies Record<CrmMatchState, string>)[item.crm_resolution.state]
}
async function load() {
  loading.value = true; error.value = ''
  try {
    if (tab.value === 'people') await refreshDirectory()
    if (tab.value === 'projects') {
      const query = new URLSearchParams({ page: String(projectPage.value), group: projectGroup.value, completeness: projectCompleteness.value })
      if (projectSearch.value.trim()) query.set('search', projectSearch.value.trim())
      if (projectStage.value) query.set('stage', projectStage.value)
      if (projectTeam.value !== null) query.set('team_id', String(projectTeam.value))
      if (projectManager.value !== null) query.set('manager_id', String(projectManager.value))
      const page = await api<Page<ProjectWorkspaceSummary> & { group_counts?: { with_data: number; without_data: number } }>(client.value ? '/projects/' : `/projects/?${query}`)
      projects.value = page.results; projectCount.value = page.count; projectNext.value = Boolean(page.next)
      if (page.group_counts) projectCounts.value = { ...page.group_counts, all: page.group_counts.with_data + page.group_counts.without_data }
    }
    if (tab.value === 'review') {
      await refreshDirectory()
      const page = await api<Page<Candidate>>(`/candidates/?status=${candidateStatus.value}&page=${candidatePage.value}${candidateFactType.value ? `&fact_type=${candidateFactType.value}` : ''}${candidateProject.value ? `&project_id=${candidateProject.value}` : ''}`)
      candidates.value = page.results; candidateCount.value = page.count; candidateNext.value = Boolean(page.next)
      crmSelections.value = {}
      for (const candidate of candidates.value) {
        const selected = candidate.crm_resolution.options.find(option => option.selection_state === 'selected')
        if (selected) crmSelections.value[candidate.id] = selected.id
      }
    }
    if (tab.value === 'tasks') commitments.value = await api<CommitmentData>('/commitments/')
    if (tab.value === 'notifications') await fetchNotifications()
  } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка загрузки' } finally { loading.value = false }
}
function catalogLabel(state: string) { return ({ idle: 'Справочник ещё не загружен', queued: 'Загрузка CRM в очереди', running: 'Загрузка CRM выполняется', succeeded: 'Справочник CRM загружен', error: 'Ошибка загрузки CRM' } as Record<string, string>)[state] || 'Состояние загрузки неизвестно' }
function catalogError(code: string) { return ({ crm_team_mapping_required: 'Не настроена команда CRM', crm_import_disabled: 'Чтение каталога CRM отключено', crm_not_configured: 'Не настроено подключение к CRM', crm_project_scope_conflict: 'Сделка уже принадлежит другой команде' } as Record<string, string>)[code] || 'Не удалось загрузить данные; повторите загрузку' }
async function refreshDirectory() {
  const first = await api<Directory>('/directory/')
  const entries = [...first.projects]
  let next = first.projects_next_page
  while (next) {
    const batch = await api<Directory>(`/directory/?project_page=${next}`)
    entries.push(...batch.projects)
    next = batch.projects_next_page
  }
  directory.value = { ...first, projects: entries }
}
async function syncCatalog(teamId: number) { await perform(() => post('/directory/crm-sync/', { team_id: teamId }), 'Загрузка CRM поставлена в очередь. Обновите справочник после обработки.') }
async function syncParticipants(configId: number) { await perform(() => post('/directory/participants-sync/', { config_id: configId }), 'Восстановление участников запущено. Обновите список после обработки.') }
function participantSyncLabel(state: string) { return ({ not_started: 'Ещё не загружено', pending: 'В очереди', enqueued: 'В очереди', processing: 'Загружаем участников', done: 'Участники загружены', failed: 'Ошибка загрузки', cancelled: 'Загрузка отменена' } as Record<string, string>)[state] || 'Ожидает повторной загрузки' }
async function retryCrm(teamId: number, candidateId?: number) {
  let queued = 0, skipped = 0
  await perform(async () => {
    const result = await post<{ queued: number; skipped: number }>('/candidates/crm-retry/', { team_id: teamId, ...(candidateId !== undefined ? { candidate_id: candidateId } : {}) })
    queued = result.queued; skipped = result.skipped
  }, () => `В очередь поиска CRM добавлено: ${queued}. Пропущено без доступного источника или признаков объекта/компании: ${skipped}. Обновите список после обработки.`)
}
async function selectTab(value: string) { tab.value = value; notice.value = ''; await load() }
async function perform(work: () => Promise<unknown>, message: string | (() => string) = 'Сохранено') { busy.value = true; error.value = ''; notice.value = ''; try { await work(); notice.value = typeof message === 'function' ? message() : message; await refreshDirectory(); await load() } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка операции' } finally { busy.value = false } }
async function reviewItem(item: Candidate, action: string) {
  await perform(async () => {
    await post(`/candidates/${item.id}/review/`, { action, base_version: item.base_version, reason: reasons.value[item.id] || '', changes: {} })
    editing.value = null
  }, action === 'approve' ? 'Факт подтверждён' : 'Предложение отклонено')
}
async function saveCorrection(item: Candidate, changes: Record<string, unknown>, reason: string) {
  await perform(async () => { await post(`/candidates/${item.id}/review/`, { action: 'approve', base_version: item.base_version, reason, changes }); editing.value = null }, 'Исправления сохранены, факт подтверждён')
}
async function matchItem(item: Candidate) { const p = directory.value.projects.find(p => p.id === matches.value[item.id]); if (p) await perform(() => post(`/candidates/${item.id}/review/`, { action: 'match', project_id: p.id, base_version: p.version, reason: reasons.value[item.id] || '' })) }
async function matchCrm(item: Candidate) {
  const crmMatchId = crmSelections.value[item.id]
  if (!crmMatchId || !reasons.value[item.id]?.trim()) return
  const selected = item.crm_resolution.options.find(option => option.id === crmMatchId)
  if (!selected) return
  await perform(() => post(`/candidates/${item.id}/review/`, {
    action: 'match_crm',
    crm_match_id: crmMatchId,
    crm_match_revision: item.crm_resolution.revision,
    base_version: selected.project_version ?? item.base_version,
    reason: reasons.value[item.id],
  }), 'CRM-сделка выбрана; подтвердите AI-факт отдельно.')
}
async function taskAction(item: CommitmentItem, action: string) { await perform(() => post(`/commitments/${item.id}/action/`, { action, version: item.version, reason: reasons.value[item.id] || '', ...(action === 'postpone' && deadlines.value[item.id] ? { deadline_at: new Date(`${deadlines.value[item.id]}:00+06:00`).toISOString() } : {}) })) }
async function assignProject(project: ManagerProjectSummary) { await perform(() => post(`/projects/${project.id}/assign/`, { manager_id: assignments.value[project.id], base_version: project.version, reason: reasons.value[project.id], transfer_open_commitments: transferTasks.value[project.id] || false })) }
async function openText(title: string, work: () => Promise<unknown>) { error.value = ''; try { const result = await work(); modalTitle.value = title; modalText.value = typeof result === 'string' ? result : JSON.stringify(result, null, 2); dialog.value?.showModal() } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка загрузки' } }

async function showHistory(id: number) { await openText('История подтверждений', () => api(`/projects/${id}/history/`)) }
async function sendNotice() { await perform(() => dispatchNotification(noticeForm.value), 'Уведомление сохранено; доставка отслеживается отдельно') }
onMounted(async () => { try { await refreshDirectory() } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка' }; await load() })
</script>
