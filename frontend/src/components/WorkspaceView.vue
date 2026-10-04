<template>
<section class="max-w-6xl mx-auto w-full p-4 md:p-6 space-y-5">
  <header><p class="text-xs text-indigo-300">{{ client ? 'КАБИНЕТ КЛИЕНТА' : 'РАБОЧИЙ КАБИНЕТ' }}</p><h1 class="text-2xl font-semibold">{{ currentUser?.name }}</h1><p class="text-sm text-slate-400">{{ currentUser?.roles.map(role => roleLabels[role]).join(' · ') }}</p></header>
  <nav class="flex gap-2 flex-wrap" aria-label="Разделы кабинета"><button v-for="item in tabs" :key="item.id" class="btn" :class="tab === item.id ? 'bg-indigo-600 border-indigo-400' : ''" @click="selectTab(item.id)">{{ item.label }}</button></nav>
  <p v-if="error" role="alert" class="panel text-rose-300">{{ error }}</p><p v-if="notice" role="status" class="text-emerald-300">{{ notice }}</p><p v-if="loading" role="status">Загрузка…</p>
  <template v-if="tab === 'projects'">
    <div class="grid md:grid-cols-2 gap-4"><article v-for="project in projects" :key="project.id" class="panel space-y-2"><h2 class="font-semibold">{{ project.name }}</h2><p>{{ project.status }} · Версия {{ project.version }}</p><template v-if="!client"><p>Договор: {{ project.contract_formatted }}</p><p>Поступило: {{ project.paid_formatted }}</p><p>Остаток: {{ project.due_formatted }}</p><button class="btn" @click="showHistory(project.id)">История подтверждений</button><details v-if="lead" class="pt-2"><summary>Сменить ответственного</summary><select class="field" v-model="assignments[project.id]"><option v-for="profile in directory.profiles" :key="profile.id" :value="profile.id">{{ profile.full_name }}</option></select><input class="field" v-model="reasons[project.id]" placeholder="Причина смены ответственного" /><label class="block"><input type="checkbox" v-model="transferTasks[project.id]" /> Передать открытые обязательства</label><button class="btn" :disabled="busy || !assignments[project.id] || !reasons[project.id]" @click="assignProject(project)">Сохранить ответственного</button></details></template></article></div><p v-if="!projects.length && !loading" class="panel">Нет доступных проектов.</p>
  </template>
  <template v-if="tab === 'review'">
    <p class="text-sm text-slate-400">AI предлагает изменения. Утверждённые суммы остаются прежними до подтверждения. Платежи проверяет финансист.</p>
    <label>Статус <select class="field" v-model="candidateStatus" @change="candidatePage = 1; load()"><option value="pending">На проверке</option><option value="approved">Принято</option><option value="rejected">Отклонено</option><option value="superseded">Заменено</option></select></label>
    <label class="ml-3">Тип <select class="field" v-model="candidateFactType" @change="candidatePage = 1; load()"><option value="">Все факты</option><option value="project">Проекты — проверить и создать</option><option value="commitment">Обязательства</option><option value="payment">Платежи</option></select></label>
    <section class="panel space-y-2" aria-label="Справочник проектов CRM">
      <p v-for="state in directory.crm_catalog || []" :key="state.team_id" :class="state.state === 'error' ? 'text-rose-300' : 'text-slate-300'">
        {{ catalogLabel(state.state) }} · Загружено: {{ state.imported_count }}
        <span v-if="state.last_success_at"> · Последняя загрузка: {{ new Date(state.last_success_at).toLocaleString('ru-RU') }}</span>
        <span v-if="state.error_code"> · {{ catalogError(state.error_code) }}</span>
        <button v-if="lead" class="btn ml-2" :disabled="busy || ['queued', 'running'].includes(state.state)" @click="syncCatalog(state.team_id)">Загрузить из CRM</button>
      </p>
      <p v-if="!directory.projects.length">Доступных проектов пока нет. Справочник загружается из Bitrix CRM; общие обязательства команды можно подтверждать без проекта.</p>
      <p v-else>Проекты из CRM доступны для привязки. Финансовые данные подтверждаются отдельно.</p>
      <button class="btn" :disabled="busy || loading" @click="refreshDirectory">Обновить справочник</button>
    </section>
    <p class="text-sm text-slate-400">Предложений: {{ candidateCount }}. Связанные факты на этой странице собраны по проекту или чату.</p>
    <template v-for="group in candidateGroups" :key="group.key">
    <h2 class="text-lg font-semibold pt-3">{{ group.label }} <span class="text-sm text-slate-400">· {{ group.items.length }} на этой странице</span></h2>
    <article v-for="item in group.items" :key="item.id" class="panel space-y-4" :data-testid="`candidate-${item.id}`">
      <p v-if="item.thread" class="text-sm text-indigo-300">Тема #{{ item.thread.id }}: {{ item.thread.topic }} · Версия {{ item.thread.version }}</p>
      <FactSummary :item="item" />
      <section class="space-y-2"><h3 class="text-sm font-semibold">Чем подтверждается факт</h3>
      <blockquote v-for="evidence in item.evidence" :key="evidence.id" class="border-l-2 border-indigo-400 pl-3 text-sm whitespace-pre-wrap"><strong v-if="evidence.role" class="block text-xs text-indigo-200">{{ ({request: 'Просьба / предмет задачи', promise: 'Обещание / назначение', deadline: 'Срок', fulfillment: 'Доказательство выполнения'} as Record<string, string>)[evidence.role] || 'Первоисточник' }}</strong>{{ evidence.quote }}</blockquote>
      <p v-if="!item.evidence.length" class="text-amber-300 text-sm">Первоисточник недоступен. Подтверждение невозможно без доступа к нему.</p></section>
      <FactConversation :candidate-id="item.id" />
      <label v-if="item.status === 'pending' && (canReview(item) || canApprove(item) || canSelectCrm(item))" class="block text-sm">Основание / причина<input class="field w-full mt-1" v-model="reasons[item.id]" /></label>
      <section v-if="item.fact_type === 'project' || item.crm_resolution.state !== 'not_requested'" class="rounded-xl border border-sky-700/60 bg-sky-950/20 p-3 space-y-3" :data-crm-state="item.crm_resolution.state">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 class="font-semibold text-sky-200">Сопоставление с CRM</h3>
            <p class="text-sm" :class="crmStateTone(item.crm_resolution.state)">{{ crmStateLabel(item) }}</p>
          </div>
          <span class="text-xs text-slate-400">Проверка №{{ item.crm_resolution.revision }}<template v-if="item.crm_resolution.checked_at"> · {{ new Date(item.crm_resolution.checked_at).toLocaleString() }}</template></span>
        </div>
        <p v-if="item.crm_resolution.error_code" class="text-sm text-rose-300">{{ crmErrors[item.crm_resolution.error_code] || 'Не удалось проверить сделку. Повторите обработку позже или обратитесь к администратору.' }}</p>
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
        <details v-if="canReview(item) && directory.projects.some(p => p.team_id === item.team_id)"><summary class="text-sm text-slate-400 cursor-pointer">Выбрать существующий проект / обновить данные перед проверкой</summary><div class="flex gap-2 flex-wrap mt-2"><select class="field" v-model="matches[item.id]" aria-label="Сопоставить с проектом"><option :value="undefined">Выберите проект</option><option v-for="p in directory.projects.filter(p => p.team_id === item.team_id)" :key="p.id" :value="p.id">{{ p.name }}</option></select><button class="btn" :disabled="busy || !matches[item.id] || !reasons[item.id]?.trim()" @click="matchItem(item)">Связать с проектом и обновить</button></div></details>
        <FactCorrectionForm v-if="canReview(item) && editing === item.id" :key="item.id" :item="item" :busy="busy" :initial-reason="reasons[item.id] || ''" :can-approve="Boolean(item.evidence.length && item.source_available !== false && canApprove(item))" @cancel="editing = null" @save="(changes, reason) => saveCorrection(item, changes, reason)" />
      </template><p v-else class="text-sm text-slate-400">{{ statusLabels[item.status] }}<template v-if="item.review_reason"> · {{ item.review_reason }}</template></p>
    </article></template>
    <nav class="flex gap-3 items-center" aria-label="Страницы предложений"><button class="btn" :disabled="loading || busy || candidatePage === 1" @click="changeCandidatePage(-1)">Предыдущая</button><span class="text-sm">Страница {{ candidatePage }}</span><button class="btn" :disabled="loading || busy || !candidateNext" @click="changeCandidatePage(1)">Следующая</button></nav><p v-if="!candidates.length && !loading" class="panel">Предложений с этим статусом нет.</p>
  </template>
  <ThreadBrowser v-if="tab === 'threads'" />
  <template v-if="tab === 'tasks'">
    <p class="text-sm text-slate-400">Сроки указаны в UTC+6. Чтение уведомления не закрывает обязательство. Перенос сохраняет исходный срок.</p>
    <article v-for="item in commitments?.commitments" :key="item.id" class="panel space-y-3"><h2 class="font-semibold">{{ item.project_name }} · {{ item.manager_name }}</h2><p>{{ item.text }}</p><p :class="item.status_color === 'red' ? 'text-rose-300' : 'text-slate-400'">{{ item.deadline_formatted }} · {{ item.status }}</p><p v-if="item.postponed_reason">Причина переноса: {{ item.postponed_reason }}</p><div v-if="['pending','overdue'].includes(item.status_code)" class="flex flex-wrap gap-2"><button class="btn-primary" :disabled="busy" @click="taskAction(item, 'fulfill')">Выполнено</button><button class="btn" :disabled="busy" @click="taskAction(item, 'help')">Нужна помощь</button><button v-if="lead" class="btn" :disabled="busy" @click="taskAction(item, 'postpone')">Перенести</button><input type="datetime-local" class="field" v-if="lead" v-model="deadlines[item.id]" aria-label="Новый срок" /><input class="field flex-1" v-model="reasons[item.id]" placeholder="Причина переноса или запрос помощи" /></div></article>
    <p v-if="commitments && !commitments.total_count" class="panel">Подтверждённых обязательств пока нет.</p>
  </template>
  <template v-if="tab === 'finance' && financeData">
    <div class="flex justify-between flex-wrap gap-2"><p>Просрочено по графику: {{ financeData.receivables.overdue }} {{ financeData.receivables.currency }}</p><button class="btn" :disabled="busy" @click="exportPayments">Выгрузить платежи CSV</button></div>
    <p class="text-amber-300 text-sm">Проектов без подтверждённого графика: {{ financeData.receivables.unknown_schedule_projects }}</p>
    <div class="panel overflow-auto"><h2 class="font-semibold mb-3">Реестр платежей</h2><table class="data-table"><thead><tr><th>ID</th><th>Дата</th><th>Проект</th><th>Сумма</th><th>Корректировка</th></tr></thead><tbody><tr v-for="payment in financeData.payments" :key="payment.id"><td>{{ payment.id }}</td><td>{{ payment.payment_date }}</td><td>{{ projectName(payment.project_id) }}</td><td>{{ payment.amount }} {{ payment.currency }}</td><td>{{ payment.reverses_id ? `К платежу #${payment.reverses_id}` : '—' }}</td></tr></tbody></table></div>
    <form v-if="financeRole" class="panel flex flex-wrap gap-3 items-end" @submit.prevent="addSchedule"><h2 class="w-full font-semibold">Добавить согласованный срок платежа</h2><label>Проект<select class="field block" v-model="schedule.project_id" required><option v-for="p in directory.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label><label>Срок<input class="field block" type="date" v-model="schedule.due_date" required /></label><label>Сумма<input class="field block" type="text" inputmode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" v-model="schedule.amount" required /></label><button class="btn-primary" :disabled="busy">Сохранить график</button></form>
    <div class="panel overflow-auto"><h2 class="font-semibold">График платежей</h2><table class="data-table"><thead><tr><th>ID</th><th>Проект</th><th>Срок</th><th>Сумма</th><th>Остаток</th></tr></thead><tbody><tr v-for="row in financeData.receivables.rows" :key="row.id"><td>{{ row.id }}</td><td>{{ projectName(row.project_id) }}</td><td>{{ row.due_date }}</td><td>{{ row.amount }}</td><td>{{ row.remaining }}</td></tr></tbody></table></div>
    <form v-if="financeRole" class="panel flex flex-wrap gap-3 items-end" @submit.prevent="allocate"><h2 class="w-full font-semibold">Распределить платёж</h2><label>Платёж<select class="field block" v-model="allocation.payment_id" required><option v-for="p in financeData.payments" :key="p.id" :value="p.id">#{{ p.id }} · {{ p.amount }}</option></select></label><label>Строка графика<select class="field block" v-model="allocation.schedule_id" required><option v-for="s in financeData.receivables.rows" :key="s.id" :value="s.id">#{{ s.id }} · {{ s.due_date }}</option></select></label><label>Сумма<input class="field block" type="text" inputmode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" v-model="allocation.amount" required /></label><button class="btn-primary" :disabled="busy">Распределить</button></form>
    <form v-if="lead || financeRole" class="panel flex flex-wrap gap-3 items-end" @submit.prevent="saveTarget"><h2 class="w-full font-semibold">Утвердить месячный план</h2><label>Команда<select class="field block" v-model="target.team_id" required><option v-for="t in directory.teams" :key="t.id" :value="t.id">{{ t.name }}</option></select></label><label>Менеджер<select class="field block" v-model="target.profile_id" required><option v-for="p in directory.profiles" :key="p.id" :value="p.id">{{ p.full_name }}</option></select></label><label>Месяц<input class="field block" type="month" v-model="targetMonth" required /></label><label>Валюта<select class="field block" v-model="target.currency"><option>KZT</option><option>USD</option><option>EUR</option><option>RUB</option></select></label><label>План<input class="field block" type="text" inputmode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" v-model="target.amount" required /></label><button class="btn-primary" :disabled="busy">Утвердить план</button></form>
  </template>
  <template v-if="tab === 'documents'">
    <form v-if="!client" class="panel space-y-3" @submit.prevent="upload"><h2 class="font-semibold">Документ или голосовое сообщение</h2><label class="block">Проект<select class="field ml-2" v-model="uploadProject" required><option v-for="p in directory.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label><input type="file" accept="image/png,image/jpeg,application/pdf,audio/ogg,audio/wav,audio/mpeg" @change="chooseFile" required /><p class="text-xs text-slate-400">До 10 МБ. Распознанный текст требует проверки. Документы клиента публикует руководитель.</p><button class="btn-primary" :disabled="busy || !file">Загрузить</button></form>
    <article v-for="item in attachments" :key="item.id" class="panel space-y-2"><h2>Документ #{{ item.id }} · {{ projectName(item.project_id) }}</h2><p class="text-sm">{{ item.content_type }} · {{ item.state === 'unavailable' ? 'Распознавание не настроено' : item.state }}</p><div class="flex gap-2 flex-wrap"><button class="btn" @click="showAttachment(item.id)">Открыть распознанный текст</button><button class="btn" @click="downloadAttachment(item.id)">Скачать файл</button><button v-if="lead" class="btn" @click="publish(item)">{{ item.published_to_client ? 'Скрыть от клиента' : 'Опубликовать клиенту' }}</button></div></article>
  </template>
  <template v-if="tab === 'notifications'">
    <button class="btn" @click="markAllAsRead">Прочитать все</button><article v-for="item in notifications" :key="item.id" class="panel space-y-2"><h2 class="font-semibold">{{ item.title }}</h2><p>{{ item.message }}</p><p class="text-xs text-slate-400">{{ new Date(item.created_at).toLocaleString() }} · {{ item.is_read ? 'Прочитано' : 'Новое' }}</p><p v-for="(delivery, index) in item.deliveries" :key="index" class="text-xs">WhatsApp: {{ deliveryLabel(delivery.state) }}</p><button v-if="!item.acknowledged" class="btn" @click="acknowledge(item.id)">Подтвердить получение</button></article>
    <form v-if="lead || financeRole" @submit.prevent="sendNotice" class="panel space-y-3"><h2 class="font-semibold">Адресное уведомление</h2><label class="block">Получатель<select class="field ml-2" v-model="noticeForm.recipient_id" required><option v-for="p in directory.profiles" :key="p.id" :value="p.user_id">{{ p.full_name }}</option></select></label><input class="field w-full" v-model="noticeForm.title" placeholder="Заголовок" required maxlength="255" /><textarea class="field w-full" v-model="noticeForm.message" placeholder="Сообщение" required maxlength="4000" /><label class="block"><input type="checkbox" v-model="noticeForm.send_whatsapp" /> Продублировать в WhatsApp</label><button class="btn-primary" :disabled="busy">Отправить уведомление</button></form>
  </template>
  <template v-if="tab === 'operations' && health"><div class="panel"><h2>Очереди и интеграции</h2><p>AI за сутки: {{ health.ai_last_24h.requests }} запросов, ошибок {{ health.ai_last_24h.failed_requests }}. Средняя задержка: {{ Math.round(health.ai_last_24h.mean_duration_ms || 0) }} мс.</p><p>Известные расходы: {{ health.ai_last_24h.cost_usd ?? "Нет данных" }} USD. Запросов без цены поставщика: {{ health.ai_last_24h.unknown_cost_requests }}.</p><p>Возраст старейшего события: {{ Math.round(health.oldest_pending_seconds) }} сек.</p><table class="data-table"><thead><tr><th>Очередь</th><th>Состояние</th><th>Количество</th></tr></thead><tbody><tr v-for="(item, i) in health.outbox" :key="i"><td>{{ item.event_type }}</td><td>{{ item.state }}</td><td>{{ item.count }}</td></tr></tbody></table><div v-for="item in health.errors" :key="item.id" class="text-amber-300 space-y-2 mt-3"><p>#{{ item.id }} {{ item.event_type }}: {{ item.error_code }}</p><template v-if="!['otp','waha_control'].includes(item.event_type)"><input class="field" v-model="reasons[item.id]" placeholder="Причина повторной обработки" /><label v-if="item.state === 'unknown' && item.event_type === 'notification'" class="block"><input type="checkbox" v-model="confirmedUndelivered[item.id]" /> Проверено у поставщика: сообщение не было доставлено</label><button class="btn" :disabled="busy || !reasons[item.id]" @click="retryEvent(item.id)">Повторить после сверки</button></template></div></div><a class="btn inline-block" href="/admin/">Администрирование доступов и интеграций</a></template>
  <template v-if="tab === 'legacy'"><p class="panel text-amber-300">Исторические записи исключены из KPI до проверки. Старый накопленный итог не создаёт платёж; каждую оплату подтвердите отдельно по дате и документу.</p><article v-for="project in legacy" :key="project.id" class="panel space-y-3"><h2>{{ project.name }}</h2><p>Договор из истории: {{ project.contract_amount }} {{ project.currency }}. Прежний накопленный итог: {{ project.legacy_paid_amount }}.</p><select class="field" v-model="legacyTeams[project.id]"><option v-for="team in directory.teams" :key="team.id" :value="team.id">{{ team.name }}</option></select><input class="field w-full" v-model="reasons[project.id]" placeholder="Основание проверки договора" /><button class="btn" :disabled="busy || !reasons[project.id] || !(project.team_id || legacyTeams[project.id])" @click="proposeLegacy(project)">Передать договор на проверку</button></article><p v-if="!legacy.length">Исторических проектов для сверки нет.</p></template>
  <dialog ref="dialog" class="w-[min(90vw,800px)] rounded-2xl bg-slate-900 text-slate-100 border border-slate-600 p-6 backdrop:bg-black/70"><div class="flex justify-between gap-3 mb-4"><h2 class="font-semibold">{{ modalTitle }}</h2><button class="btn" @click="dialog?.close()">Закрыть</button></div><pre class="whitespace-pre-wrap break-words text-sm max-h-[65vh] overflow-auto">{{ modalText }}</pre></dialog>
</section>
</template>
<script setup lang="ts">
import ThreadBrowser from './review/ThreadBrowser.vue'
import { ref, computed, onMounted } from 'vue'
import FactSummary from './review/FactSummary.vue'
import FactConversation from './review/FactConversation.vue'
import FactCorrectionForm from './review/FactCorrectionForm.vue'
import { roles as roleLabels, statusLabels, crmErrors, crmReasons } from './review/presentation'
import { api, post, pollOperation, download } from '../composables/api'
import { currentUser } from '../composables/session'
import { useNotifications } from '../composables/useNotifications'
import type { Page, Candidate, CrmMatchState, Directory, Finance, Attachment, OperationReceipt, ExportResult, Health, LegacyProject } from '../types/platform'
import type { ManagerProjectSummary, CommitmentData, CommitmentItem } from '../types/chat'
const roles = computed(() => currentUser.value?.roles || [])
const client = computed(() => roles.value.includes('client')), lead = computed(() => roles.value.includes('team_lead')), financeRole = computed(() => roles.value.includes('finance'))
const tab = ref('projects'), loading = ref(false), busy = ref(false), error = ref(''), notice = ref('')
const tabs = computed(() => [{ id: 'projects', label: 'Проекты' }, ...(!client.value ? [{ id: 'review', label: 'Проверка фактов' }, { id: 'threads', label: 'Темы переписки' }, { id: 'tasks', label: 'Обязательства' }, { id: 'finance', label: 'Финансы' }] : []), { id: 'documents', label: 'Документы' }, { id: 'notifications', label: 'Уведомления' }, ...(roles.value.includes('admin') ? [{ id: 'operations', label: 'Состояние системы' }, { id: 'legacy', label: 'Исторические данные' }] : [])])
const confirmedUndelivered = ref<Record<number, boolean>>({})
const assignments = ref<Record<number, number>>({}), transferTasks = ref<Record<number, boolean>>({})
const legacy = ref<LegacyProject[]>([]), legacyTeams = ref<Record<number, number>>({})
const projects = ref<ManagerProjectSummary[]>([]), candidates = ref<Candidate[]>([]), commitments = ref<CommitmentData | null>(null), financeData = ref<Finance | null>(null)
const attachments = ref<Attachment[]>([]), health = ref<Health | null>(null), directory = ref<Directory>({ teams: [], profiles: [], projects: [] })
const candidateFactType = ref(''), candidateStatus = ref('pending'), editing = ref<number | null>(null), reasons = ref<Record<number, string>>({}), deadlines = ref<Record<number, string>>({}), matches = ref<Record<number, number>>({})
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
const schedule = ref({ project_id: 0, due_date: '', amount: '' }), allocation = ref({ payment_id: 0, schedule_id: 0, amount: '' })
const target = ref({ team_id: 0, profile_id: 0, amount: '', currency: 'KZT' }), targetMonth = ref(new Date().toISOString().slice(0, 7))
const uploadProject = ref(0), file = ref<File | null>(null)
const noticeForm = ref({ recipient_id: 0, title: '', message: '', send_whatsapp: false })
const dialog = ref<HTMLDialogElement>(), modalTitle = ref(''), modalText = ref('')
const { notifications, fetchNotifications, markAllAsRead, acknowledge, dispatchNotification } = useNotifications()
function projectName(id: number) { return directory.value.projects.find(p => p.id === id)?.name || projects.value.find(p => p.id === id)?.name || `#${id}` }
function canReview(item: Candidate) {
  const financialProject = item.fact_type === 'project' && ('contract_amount' in item.proposed_changes || 'cost_amount' in item.proposed_changes)
  return item.fact_type === 'payment' || financialProject ? financeRole.value : lead.value
}
function canSelectCrm(_item: Candidate) { return lead.value }
function requiresFinanceForCrmMaterialization(item: Candidate) {
  const selected = item.crm_resolution.options.find(option => option.selection_state === 'selected')
  if (!selected) return false
  return item.crm_resolution.state === 'matched'
    && item.project_id === null
    && selected.project_id === null
    && selected.opportunity !== null
}
function canApprove(item: Candidate) { return requiresFinanceForCrmMaterialization(item) ? financeRole.value : canReview(item) }
function approvalHint(item: Candidate) {
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
    if (tab.value === 'legacy') legacy.value = (await api<Page<LegacyProject>>('/legacy/projects/')).results
    if (tab.value === 'projects') projects.value = (await api<Page<ManagerProjectSummary>>('/projects/')).results
    if (tab.value === 'review') {
      await refreshDirectory()
      const page = await api<Page<Candidate>>(`/candidates/?status=${candidateStatus.value}&page=${candidatePage.value}${candidateFactType.value ? `&fact_type=${candidateFactType.value}` : ''}`)
      candidates.value = page.results; candidateCount.value = page.count; candidateNext.value = Boolean(page.next)
      crmSelections.value = {}
      for (const candidate of candidates.value) {
        const selected = candidate.crm_resolution.options.find(option => option.selection_state === 'selected')
        if (selected) crmSelections.value[candidate.id] = selected.id
      }
    }
    if (tab.value === 'tasks') commitments.value = await api<CommitmentData>('/commitments/')
    if (tab.value === 'finance') financeData.value = await api<Finance>('/finance/')
    if (tab.value === 'documents') attachments.value = await api<Attachment[]>('/attachments/')
    if (tab.value === 'notifications') await fetchNotifications()
    if (tab.value === 'operations') health.value = await api<Health>('/operations-health/')
  } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка загрузки' } finally { loading.value = false }
}
function catalogLabel(state: string) { return ({ idle: 'Справочник ещё не загружен', queued: 'Загрузка CRM в очереди', running: 'Загрузка CRM выполняется', succeeded: 'Справочник CRM загружен', error: 'Ошибка загрузки CRM' } as Record<string, string>)[state] || 'Состояние загрузки неизвестно' }
function catalogError(code: string) { return ({ crm_team_mapping_required: 'Не настроена команда CRM', crm_import_disabled: 'Импорт CRM отключён', crm_not_configured: 'Не настроено подключение к CRM', crm_project_scope_conflict: 'Сделка уже принадлежит другой команде' } as Record<string, string>)[code] || 'Не удалось загрузить данные; повторите загрузку' }
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
async function selectTab(value: string) { tab.value = value; notice.value = ''; await load() }
async function perform(work: () => Promise<unknown>, message = 'Сохранено') { busy.value = true; error.value = ''; notice.value = ''; try { await work(); notice.value = message; await refreshDirectory(); await load() } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка операции' } finally { busy.value = false } }
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
async function proposeLegacy(project: LegacyProject) { await perform(() => post('/candidates/manual/', { project_id: project.id, team_id: project.team_id || legacyTeams.value[project.id], reason: reasons.value[project.id], changes: { fact_type: 'project', contract_amount: project.contract_amount } }), 'Предложение создано. Подтвердите проверенные значения в разделе проверки фактов.') }
async function retryEvent(id: number) { await perform(() => post(`/outbox/${id}/retry/`, { reason: reasons.value[id], provider_confirmed_not_delivered: confirmedUndelivered.value[id] || false }), 'Повтор поставлен в очередь') }
async function addSchedule() { await perform(() => post('/finance/schedules/', schedule.value)) }
async function allocate() { await perform(() => post('/finance/allocations/', allocation.value)) }
async function saveTarget() { const month = `${targetMonth.value}-01`; const old = financeData.value?.targets.find(t => t.team_id === target.value.team_id && t.currency === target.value.currency && t.profile_id === target.value.profile_id && t.month === month); await perform(() => post('/finance/targets/', { ...target.value, month, base_version: old?.version || 0 })) }
async function openText(title: string, work: () => Promise<unknown>) { error.value = ''; try { const result = await work(); modalTitle.value = title; modalText.value = typeof result === 'string' ? result : JSON.stringify(result, null, 2); dialog.value?.showModal() } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка загрузки' } }

async function showHistory(id: number) { await openText('История подтверждений', () => api(`/projects/${id}/history/`)) }
function chooseFile(event: Event) { file.value = (event.target as HTMLInputElement).files?.[0] || null }
async function upload() { if (!file.value) return; const form = new FormData(); form.append('project_id', String(uploadProject.value)); form.append('file', file.value); await perform(() => api('/attachments/', { method: 'POST', body: form }), 'Файл принят в обработку') }
async function showAttachment(id: number) { await openText('Распознанный текст', async () => (await api<{ transcript: string }>(`/attachments/${id}/`)).transcript || 'Распознанного текста пока нет.') }
async function downloadAttachment(id: number) { await perform(() => download(`/attachments/${id}/?download=1`, `document-${id}`), 'Файл скачан') }
async function publish(item: Attachment) { await perform(() => post(`/attachments/${item.id}/`, { published_to_client: !item.published_to_client })) }
async function sendNotice() { await perform(() => dispatchNotification(noticeForm.value), 'Уведомление сохранено; доставка отслеживается отдельно') }
async function exportPayments() { await perform(async () => { const receipt = await post<OperationReceipt>('/exports/', { idempotency_key: crypto.randomUUID() }); const result = await pollOperation<ExportResult>(receipt.operation_id); const url = URL.createObjectURL(new Blob(['\uFEFF', result.content], { type: 'text/csv;charset=utf-8' })); const a = document.createElement('a'); a.href = url; a.download = result.filename; a.click(); URL.revokeObjectURL(url) }, 'Выгрузка готова') }
onMounted(async () => { try { await refreshDirectory(); uploadProject.value = directory.value.projects[0]?.id || 0 } catch (e) { error.value = e instanceof Error ? e.message : 'Ошибка' }; await load() })
</script>
