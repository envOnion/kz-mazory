"""Read-only business overview: one authorized source cohort and its outcomes."""
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection, transaction
from django.db.models import BigIntegerField, Exists, F, Func, JSONField, Max, Min, OuterRef, Q, Subquery
from django.db.models.expressions import RawSQL
from django.db.models.functions import Lower, TruncDate, Trim
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .communication import sanitize_body
from .messenger_api import _current_axes_cte, record_href, records_scope
from .models import (AISettings, BitrixDealChangeLog, BitrixSettings, DialogueThread, FactCandidate,
                     FactClarification, FactDecision, FactEvidence, MessageProcessingTrace, ObjectIdentity,
                     CommunicationRecord, OutboxEvent, RawMessage, SourceWorkItem, Team, ThreadMessage, WhatsAppHistoryRun)
from .operation_scopes import crm_scope

GOOD_CRM = ('delivered', 'confirmed')
OPEN_CRM = ('pending', 'queued', 'enqueued', 'sending', 'accepted', 'processing', 'retrying', 'retry_wait')
OPEN_QUESTIONS = ('queued', 'awaiting', 'answered', 'unconfirmed', 'expired')
SOURCE_TYPES = ('waha', 'whatsapp_export')
PAGE_SIZE = 30


def _ident(params, name):
    value = params.get(name)
    if not value:
        return None
    if not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise ValidationError({'error': f'Некорректный ID: {name}.'})
    return int(value)


def _date(value, name):
    try:
        parsed = parse_datetime(value) if value else None
    except (ValueError, TypeError):
        parsed = None
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationError({'error': f'{name}: укажите дату ISO 8601 с часовым поясом.'})
    return parsed


def _iso(value):
    return value.isoformat() if value else None


def overview_axes(qs):
    """Canonical latest state, corrected once for unfinished source processing."""
    sql, params = _current_axes_cte(qs)
    record_table = connection.ops.quote_name(CommunicationRecord._meta.db_table)
    raw_table = connection.ops.quote_name(RawMessage._meta.db_table)
    sql += f""", overview_axes AS MATERIALIZED (
        SELECT axes.id, raw.received_at,
            CASE WHEN NOT raw.processed AND axes.analysis IN ('analyzed', 'no_crm_actions')
                 THEN 'queued' ELSE axes.analysis END AS analysis
        FROM current_axes AS axes
        INNER JOIN {record_table} AS record ON record.id = axes.id
        INNER JOIN {raw_table} AS raw ON raw.id = record.raw_message_id
    ) """
    return sql, params


def overview_match_ids(qs, states):
    sql, params = overview_axes(qs)
    return RawSQL(sql + 'SELECT id FROM overview_axes WHERE analysis = ANY(%s)',
                  [*params, list(states)], output_field=BigIntegerField())


def overview_state_totals(qs):
    sql, params = overview_axes(qs)
    sql += """SELECT analysis, COUNT(*),
        MIN(received_at) FILTER (WHERE analysis IN ('received', 'queued', 'analyzing'))
        FROM overview_axes GROUP BY analysis"""
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    oldest = min((row[2] for row in rows if row[2] is not None), default=None)
    return {state: total for state, total, _ in rows}, oldest


def cohort_candidates(user, raw_ids, team_id=None):
    evidence = FactEvidence.objects.filter(raw_message_id__in=raw_ids).values('candidate_id')
    qs = access.candidates_for(user).filter(
        Q(pk__in=evidence) | Q(trace__raw_message_id__in=raw_ids),
    ).exclude(status__in=['rejected', 'superseded'])
    newer = MessageProcessingTrace.objects.filter(
        raw_message_id=OuterRef('trace__raw_message_id'), pk__gt=OuterRef('trace_id'))
    qs = qs.alias(newer_attempt=Exists(newer)).exclude(status='pending', newer_attempt=True)
    return qs.filter(team_id=team_id) if team_id else qs


def cohort_deliveries(user, candidates):
    qs = crm_scope(user).filter(fact_event__is_superseded=False,
                                fact_event__decision__candidate__in=candidates)
    latest = crm_scope(user).filter(
        external_object_link_id=OuterRef('external_object_link_id'),
        target_version=OuterRef('target_version'), pk__gt=OuterRef('pk'))
    receipts = BitrixDealChangeLog.objects.filter(
        delivery_id=OuterRef('pk'), reconstructed=False,
        object_type=OuterRef('external_object_link__object_type'),
        status__in=['accepted', 'success', 'confirmed'])
    return qs.filter(~Exists(latest)).exclude(state__in=['superseded', 'cancelled']).annotate(
        was_created=Exists(receipts.filter(action__in=['add', 'create'])),
        was_updated=Exists(receipts.filter(action__in=['update', 'change'])),
        was_completed=Exists(receipts.filter(action='complete')))


class Selection:
    """Shared predicates for totals and details; never count a truncated page."""

    def __init__(self, request, register_only=False):
        self.user = request.user
        self.register_only = register_only
        if access.is_client(self.user):
            raise PermissionDenied('Обзор команды доступен сотрудникам.')
        params = request.query_params
        now = timezone.now()
        self.as_of = _date(params['as_of'], 'as_of') if params.get('as_of') else now
        if self.as_of > now + timedelta(seconds=5):
            raise ValidationError({'error': 'Время расчёта не может быть в будущем.'})
        self.team_id, self.chat_id = _ident(params, 'team'), _ident(params, 'chat')
        self.period = params.get('period', 'today')
        midnight = timezone.make_aware(datetime.combine(timezone.localdate(self.as_of), time.min))
        if self.period == 'custom':
            self.start = _date(params.get('from'), 'from')
            self.end = _date(params.get('to'), 'to')
        elif self.period == 'all':
            self.start, self.end = None, self.as_of
        elif self.period in ('today', '7d', '30d'):
            days = {'today': 1, '7d': 7, '30d': 30}[self.period]
            self.start, self.end = midnight - timedelta(days=days - 1), self.as_of
        else:
            raise ValidationError({'error': 'Неизвестный период.'})
        self.end = min(self.end, self.as_of)
        if self.start and self.start >= self.end:
            raise ValidationError({'error': 'Начало периода должно предшествовать окончанию.'})
        self.configs = access.configs_for(self.user)
        if self.team_id:
            self.configs = self.configs.filter(team_id=self.team_id)
        if self.chat_id:
            self.configs = self.configs.filter(pk=self.chat_id)
        self.base = records_scope(self.user).filter(
            direction='inbound', purpose='source', raw_message__source__in=SOURCE_TYPES,
            raw_message__received_at__lte=self.as_of,
        ).filter(Q(created_at__lte=self.as_of) | Q(created_at__isnull=True)).exclude(raw_message__processing_state__in=['deleted', 'superseded'])
        if self.team_id:
            self.base = self.base.filter(team_id=self.team_id)
        if self.chat_id:
            # A forged chat parameter cannot expand the source grant.
            self.base = self.base.filter(config_id__in=self.configs.values('id'))
        known = Q(occurred_at__lt=self.end, occurred_at__isnull=False,
                  time_is_estimated=False, raw_message__sent_at_known=True)
        unknown = Q(occurred_at__isnull=True) | Q(time_is_estimated=True) | Q(raw_message__sent_at_known=False)
        self.known_time = known
        self.unknown_time = unknown
        if self.start:
            known &= Q(occurred_at__gte=self.start)
        unknown &= Q(raw_message__received_at__lt=self.end)
        if self.start:
            unknown &= Q(raw_message__received_at__gte=self.start)
        self.records = self.base.filter(known | unknown)
        self.raw_ids = self.records.values('raw_message_id')
        active_events = OutboxEvent.objects.filter(
            state__in=['pending', 'enqueued', 'processing'],
            event_type__in=['extract_message', 'route_message', 'extract_thread', 'buffer_message', 'attachment', 'whatsapp_artifact'],
        ).filter(Q(payload__raw_id=Func(OuterRef('raw_message_id'), function='to_jsonb', output_field=JSONField())) |
                 Q(payload__batch_ids__contains=Func(OuterRef('raw_message_id'), function='jsonb_build_array', output_field=JSONField())))
        active_work = SourceWorkItem.objects.filter(raw_message_id=OuterRef('raw_message_id')).exclude(
            state__in=['done', 'completed', 'error', 'failed'])
        self.background = self.records.filter(Exists(active_events) | Exists(active_work))
        self.threads = DialogueThread.objects.filter(
            pk__in=ThreadMessage.objects.filter(raw_message_id__in=self.raw_ids).values('thread_id'),
        ).exclude(state='superseded')
        # Restrict every node, even when a cross-team relation is corrupt.
        self.threads = self.threads.filter(config_id__in=self.configs.values('id'))
        self.candidates = cohort_candidates(self.user, self.raw_ids, self.team_id)
        self.projects = access.projects_for(self.user, include_archived=True).filter(
            pk__in=self.candidates.values('project_id'))
        self.identities = ObjectIdentity.objects.filter(
            Q(pk__in=self.candidates.values('object_identity_id')) |
            Q(pk__in=self.threads.values('object_identity_id')) |
            Q(pk__in=self.projects.values('object_identity_id')))
        self.tasks = access.commitments_for(self.user, include_archived=True).filter(
            Q(candidate__in=self.candidates) | Q(source_message_id__in=self.raw_ids))
        self.crm = cohort_deliveries(self.user, self.candidates)
        deal_delivery = self.crm.filter(external_object_link__object_type='deal',
                                        external_object_link__local_id=OuterRef('project_id'))
        self.unscheduled_deals = self.candidates.filter(status='approved', fact_type='project', project__isnull=False).filter(
            Q(project__needs_bitrix_sync=True) | Q(project__bitrix_id__isnull=True) | Q(project__bitrix_id=''),
        ).filter(~Exists(deal_delivery))
        task_delivery = self.crm.filter(external_object_link__object_type='task',
                                        external_object_link__local_id=OuterRef('accepted_commitment__pk'))
        self.unscheduled_tasks = self.candidates.filter(status='approved', fact_type='commitment', accepted_commitment__isnull=False).exclude(
            accepted_commitment__status__in=['fulfilled', 'cancelled']).filter(
            Q(accepted_commitment__bitrix_task_id__isnull=True) | Q(accepted_commitment__bitrix_task_id=''),
        ).filter(~Exists(task_delivery))
        # M2M question membership is read via a subquery to avoid fanout.
        self.questions = FactClarification.objects.filter(
            Q(candidate__in=self.candidates) | Q(candidates__in=self.candidates),
            state__in=OPEN_QUESTIONS, config_id__in=self.configs.values('id'),
        ).distinct()
        latest_decision = FactDecision.objects.filter(candidate_id=OuterRef('pk')).order_by('-id')
        self.pending = self.candidates.filter(status='pending').annotate(
            decision_reason=Subquery(latest_decision.values('reason_code')[:1]),
            decision_explanation=Subquery(latest_decision.values('explanation')[:1]))
        self.registry = {}

    def metric(self, key, label, qs, kind, description, tone='normal', distinct=None):
        self.registry[key] = (label, qs, kind, distinct)
        value = 0 if self.register_only else qs.values(distinct).distinct().count() if distinct else qs.count()
        return {'key': key, 'label': label, 'value': value,
                'description': description, 'tone': tone}

    def messages(self):
        self.states, self.oldest_analysis = ({}, None) if self.register_only else overview_state_totals(self.records)
        definitions = [
            ('total', 'Получено сообщений', None, 'Уникальные входящие сообщения в выбранном периоде.', 'normal'),
            ('analyzed', 'Разобрано', ['analyzed', 'no_crm_actions'], 'Успешно разобраны, включая сообщения без коммерческих действий. Не число попыток.', 'normal'),
            ('no_actions', 'Без изменений CRM', ['no_crm_actions'], 'Разобраны; коммерческих действий не требуется.', 'normal'),
            ('queued', 'Ожидают обработки', ['received', 'queued'], 'Получены или поставлены в очередь.', 'normal'),
            ('processing', 'Обрабатываются', ['analyzing'], 'Анализ ещё выполняется.', 'normal'),
            ('context', 'Нужен контекст', ['need_context'], 'Ожидается уточнение или дополнительный контекст.', 'warning'),
            ('review', 'Требуют проверки', ['needs_review'], 'Автоматическое применение остановлено до проверки.', 'warning'),
            ('errors', 'Не удалось разобрать', ['error'], 'Ошибка последнего актуального результата.', 'danger'),
        ]
        result = []
        for key, label, states, description, tone in definitions:
            qs = self.records
            if states:
                qs = self.records.filter(pk__in=overview_match_ids(self.records, states))
            # Register exact predicates but reuse the single aggregate for totals.
            self.registry['messages.' + key] = (label, qs, 'record', None)
            value = sum(self.states.get(s, 0) for s in states) if states else sum(self.states.values())
            result.append({'key': 'messages.' + key, 'label': label, 'value': value,
                           'description': description, 'tone': tone})
        self.uncertain_records = self.records.filter(self.unknown_time)
        result.append(self.metric('messages.unknown_time', 'Время сообщения неизвестно',
                                  self.uncertain_records, 'record',
                                  'Показаны по времени получения; полнота периода не подтверждена.', 'warning'))
        result.append(self.metric('messages.background', 'Есть фоновые работы', self.background,
                                  'record', 'Сообщения с активными заданиями, в том числе повторным анализом. Может пересекаться с другими состояниями.'))
        self.media = self.records.filter(raw_message__artifacts__isnull=False).exclude(raw_message__artifacts__state='ready')
        result.append(self.metric('messages.media', 'Вложения недоступны', self.media, 'record',
                                  'Извлечение содержимого вложения ещё не завершено или недоступно.', 'warning'))
        waiting = self.records.filter(
            Q(pk__in=overview_match_ids(self.records, ['received', 'queued', 'analyzing'])) |
            Q(pk__in=self.background.values('pk')))
        self.registry['messages.delayed'] = ('Сообщения без завершённого результата более 30 минут',
                                             waiting.filter(raw_message__received_at__lt=self.as_of - timedelta(minutes=30)), 'record', None)
        return result

    def dialogues(self):
        known = self.threads.annotate(clean_topic=Lower(Trim('topic'))).exclude(
            clean_topic__in=['', 'тема требует уточнения', 'общее обсуждение', 'неизвестная тема', 'unknown']).exclude(state='unknown')
        missing = self.threads.exclude(pk__in=known.values('pk'))
        context = missing.filter(state='open')
        failed = missing.exclude(state='open')
        return [self.metric('dialogues.total', 'Выявлено обсуждений', self.threads, 'thread', 'Отдельные активные обсуждения внутри чатов, связанные с сообщениями периода.'),
                self.metric('dialogues.topic', 'Тема определена', known, 'thread', 'Содержательная тема есть; состояние не unknown.'),
                self.metric('dialogues.context', 'Для темы нужен контекст', context, 'thread', 'Открытое обсуждение без определённой темы.', 'warning'),
                self.metric('dialogues.unknown', 'Тему определить не удалось', failed, 'thread', 'Тема отсутствует или результат определения неизвестен.', 'warning'),
                self.metric('dialogues.linked', 'Связаны со сделкой', self.threads.filter(project__in=access.projects_for(self.user, include_archived=True)), 'thread', 'Привязка к доступной сделке, отдельно от определения темы.'),
                self.metric('dialogues.unlinked', 'Без привязки к сделке', self.threads.filter(project__isnull=True), 'thread', 'Не каждое обсуждение требует сделки; это не техническая ошибка.')]

    def outcomes(self):
        result = []
        for kind, label, fact_type in [('deal', 'Сделки в CRM', 'project'), ('task', 'Задачи в CRM', 'commitment')]:
            deliveries = self.crm.filter(external_object_link__object_type=kind)
            good = deliveries.filter(state__in=GOOD_CRM)
            metrics = [self.metric(f'{kind}.detected', 'Выявлено изменений', self.candidates.filter(fact_type=fact_type), 'candidate', 'Текущие предложения из сообщений периода; это ещё не выполненные действия.'),
                       self.metric(f'{kind}.created', 'Создано в CRM', good.filter(was_created=True), 'crm', 'Уникальные объекты: есть журнал создания и подтверждение результата.', distinct='external_object_link_id'),
                       self.metric(f'{kind}.updated', 'Обновлено в CRM', good.filter(was_updated=True), 'crm', 'Уникальные объекты с подтверждённым изменением; повторные попытки не прибавляются.', distinct='external_object_link_id'),
                       self.metric(f'{kind}.pending', 'Ожидает внесения', deliveries.filter(state__in=OPEN_CRM), 'crm', 'Изменение подготовлено, но результат в CRM ещё не подтверждён.', distinct='external_object_link_id'),
                       self.metric(f'{kind}.unscheduled', 'Нет доставки в CRM', self.unscheduled_deals if kind == 'deal' else self.unscheduled_tasks, 'candidate', 'Сохранено в Mazory, но доставка в CRM не подготовлена. Проверьте настройки автоматического создания и причины решения.', 'warning'),
                       self.metric(f'{kind}.attention', 'Требует внимания', deliveries.exclude(state__in=GOOD_CRM + OPEN_CRM), 'crm', 'Ошибка, конфликт или неизвестный результат.', 'danger', 'external_object_link_id'),
                       self.metric(f'{kind}.unclassified', 'Подтверждено без типа действия', good.filter(was_created=False, was_updated=False, was_completed=False), 'crm', 'Результат подтверждён, но создание/обновление не доказано журналом. Возможна доставка без изменения полей.', distinct='external_object_link_id')]
            if kind == 'task':
                metrics.append(self.metric('task.completed', 'Закрыто в CRM', good.filter(was_completed=True), 'crm', 'Закрытие подтверждено журналом и состоянием доставки.', distinct='external_object_link_id'))
            result.append({'key': kind, 'label': label,
                           'description': 'Результаты по сообщениям выбранного периода. Один объект может быть создан и затем обновлён; показатели не складываются.', 'metrics': metrics})
        created = self.identities.filter(created_at__lt=self.end)
        updated = self.identities.filter(version__gt=1, updated_at__lt=self.end)
        if self.start:
            created = created.filter(created_at__gte=self.start)
            updated = updated.filter(created_at__lt=self.start, updated_at__gte=self.start)
        else:
            updated = self.identities.none()  # No lower boundary can prove a pre-existing object.
        result.append({'key': 'projects', 'label': 'Проекты и объекты в Mazory',
                       'description': 'Объекты, о которых идёт речь в переписке. Один проект может иметь несколько сделок. Создание и обновление — по времени сохранения в Mazory.',
                       'metrics': [self.metric('projects.detected', 'Выявлено проектов', self.identities, 'identity', 'Уникальные объекты, связанные с обсуждениями и фактами периода.'),
                                   self.metric('projects.created', 'Создано в Mazory', created, 'identity', 'Связанные объекты, созданные в выбранном периоде.'),
                                   self.metric('projects.updated', 'Обновлено в Mazory', updated, 'identity', 'Ранее существовавшие объекты, последнее изменение которых сохранено в выбранном периоде.'),
                                   self.metric('projects.unresolved', 'Требует уточнения', self.identities.exclude(state='confirmed'), 'identity', 'Идентичность объекта ещё не подтверждена.', 'warning')]})
        if self.start is None:
            result[-1]['metrics'][2].update(value=None, description='Полная история обновлений объектов недоступна. Выберите период, чтобы увидеть последние изменения существовавших ранее объектов.')
        return result

    def attention(self):
        questions_identity = self.questions.filter(kind='identity')
        questions_amount = self.questions.filter(kind='sale_amount')
        specs = [
            ('messages.errors', 'Ошибки разбора сообщений', None, '', 'Откройте исходные сообщения и последнее состояние анализа.', 'danger'),
            ('messages.context', 'Сообщения ожидают контекста', None, '', 'Проверьте уточнения; обработка остальных сообщений продолжается.', 'warning'),
            ('messages.review', 'Сообщения требуют проверки', None, '', 'Автоматическое применение остановлено до проверки.', 'warning'),
            ('messages.media', 'Содержимое вложений недоступно', self.media, 'record', 'Проверьте загрузку и извлечение содержимого; полнота анализа не подтверждена.', 'warning'),
            ('questions.identity', 'Уточнить сделку или объект', questions_identity, 'question', 'Показаны получатель, состояние и время отправки вопроса.', 'warning'),
            ('questions.amount', 'Уточнить сумму', questions_amount, 'question', 'Изменение суммы ожидает подтверждения человека.', 'warning'),
            ('facts.pending', 'Факты ожидают решения', self.pending, 'candidate', 'Неприменённые предложения, включая ожидание поиска CRM или проверки.', 'warning'),
            ('deal.unscheduled', 'Сделки сохранены локально без доставки CRM', self.unscheduled_deals, 'candidate', 'Проверьте настройки создания сделок и решение по факту.', 'warning'),
            ('task.unscheduled', 'Задачи сохранены локально без доставки CRM', self.unscheduled_tasks, 'candidate', 'Проверьте, включено ли автоматическое создание задач.', 'warning'),
            ('crm.errors', 'Изменения CRM не подтверждены', self.crm.exclude(state__in=GOOD_CRM + OPEN_CRM), 'crm', 'Проверьте причину конфликта или доставки; это не успешное изменение.', 'danger'),
            ('tasks.deadline', 'В обязательствах нет срока', self.tasks.filter(is_verified=True, status__in=['pending', 'overdue'], deadline_at__isnull=True, deadline__isnull=True), 'task', 'Уточните срок выполнения.', 'warning'),
            ('tasks.responsible', 'В обязательствах нет ответственного', self.tasks.filter(is_verified=True, status__in=['pending', 'overdue'], manager__isnull=True, participant__isnull=True, responsible_name=''), 'task', 'Назначьте или уточните ответственного.', 'warning'),
            ('tasks.overdue', 'Обязательства просрочены', self.tasks.filter(is_verified=True).filter(Q(status='overdue') | Q(status='pending', deadline_at__lt=self.as_of) | Q(status='pending', deadline_at__isnull=True, deadline__lt=timezone.localdate(self.as_of))), 'task', 'Проверьте исполнение или согласуйте новый срок.', 'warning'),
            ('dialogues.unknown', 'Не удалось определить тему', None, '', 'Просмотрите обсуждения без определённой темы.', 'warning'),
        ]
        result = []
        for key, label, qs, kind, description, severity in specs:
            if qs is not None:
                metric = self.metric(key, label, qs, kind, description)
                count = metric['value']
            else:
                _, qs, _, _ = self.registry[key]
                count = 0 if self.register_only else sum(self.states.get(s, 0) for s in {'messages.errors': ['error'], 'messages.context': ['need_context'], 'messages.review': ['needs_review']}.get(key, [])) if key.startswith('messages.') else qs.count()
            if count:
                result.append({'key': key, 'label': label, 'count': count,
                               'description': description, 'severity': severity})
        return result

    def sources(self):
        rows = []
        jobs = self.configs.select_related('history_job').order_by('name', 'id')
        last = dict(self.base.exclude(occurred_at__isnull=True).values('config_id').annotate(last=Max('occurred_at')).values_list('config_id', 'last'))
        full_runs = {r['job__config_id']: r for r in WhatsAppHistoryRun.objects.filter(
            job__config__in=self.configs, run_kind='full').order_by('job__config_id', '-id').distinct('job__config_id').values('job__config_id', 'state', 'cutoff_at', 'finished_at', 'source_snapshot')}
        for config in jobs:
            job = getattr(config, 'history_job', None)
            checked = job.last_checked_at if job else None
            through = job.new_messages_since if job else None
            threshold = max(300, 3 * job.new_message_poll_seconds) if job else 300
            status, description = 'unknown', 'Нет подтверждённой проверки источника.'
            if config.status != 'WORKING':
                status, description = 'disconnected', 'Подключение WhatsApp требует проверки.'
            elif job and job.enabled and checked and through:
                if checked < self.as_of - timedelta(seconds=threshold):
                    status, description = 'stale', 'Последняя проверка источника просрочена.'
                elif through < (self.end if self.end < self.as_of else self.end - timedelta(seconds=threshold)):
                    status, description = 'stale', 'Проверка не покрывает выбранную границу данных.'
                else:
                    status, description = 'fresh', 'Мониторинг источника проверен; показано время подтверждённого покрытия.'
            full = full_runs.get(config.pk)
            if full and any(full['source_snapshot'].get(key) != value for key, value in (
                    ('session_name', config.session_name), ('chat_id', config.group_jid), ('team_id', config.team_id))):
                full = None
            if full:
                description += ' Полная история: ' + {'completed': 'анализ завершён', 'completed_with_errors': 'завершена с ошибками', 'analyzing': 'анализируется', 'importing': 'загружается', 'failed': 'ошибка', 'cancelled': 'импорт отменён'}.get(full['state'], 'покрытие ещё не подтверждено') + '.'
                if full['state'] not in ('completed', 'empty'):
                    status = 'unknown' if status == 'fresh' else status
            elif self.period == 'all':
                status = 'unknown' if status == 'fresh' else status
                description += ' Полнота старой истории не подтверждена.'
            # A live monitor starting at noon cannot prove that the morning
            # (or yesterday) was imported. Bridge the gap only with a full scan.
            monitor_start = job.new_messages_started_at if job else None
            if status == 'fresh' and (self.start is None or monitor_start is None or monitor_start > self.start):
                needed_through = min(monitor_start, self.end) if monitor_start else self.end
                full_covers_gap = bool(full and full['state'] == 'completed' and full['cutoff_at'] and full['cutoff_at'] >= needed_through)
                if not full_covers_gap:
                    status = 'unknown'
                    description += ' Мониторинг не подтверждает начало выбранного периода; требуется завершённая загрузка предыдущей истории.'
            rows.append({'id': config.id, 'name': config.name, 'status': status,
                         'checked_at': _iso(checked), 'covered_through': _iso(through),
                         'last_message_at': _iso(last.get(config.id)), 'description': description})
        return rows

    def details(self, key, page):
        if key.startswith('source.'):
            config_id = _ident({'chat': key.removeprefix('source.')}, 'chat')
            source = self.configs.filter(pk=config_id).first()
            if source:
                self.registry[key] = (source.name + ': сообщения периода', self.records.filter(config_id=source.pk), 'record', None)
        if key not in self.registry:
            raise ValidationError({'error': 'Неизвестный показатель.'})
        label, qs, kind, distinct = self.registry[key]
        count = qs.values(distinct).distinct().count() if distinct else qs.count()
        if distinct:
            qs = qs.order_by(distinct, '-id').distinct(distinct)
        else:
            qs = qs.order_by('-id')
        if kind == 'record':
            qs = qs.select_related('raw_message')
        elif kind == 'thread':
            qs = qs.select_related('project')
        elif kind == 'candidate':
            qs = qs.select_related('trace__raw_message', 'project')
        elif kind == 'question':
            qs = qs.select_related('recipient', 'candidate__trace__raw_message')
        elif kind == 'crm':
            qs = qs.select_related('external_object_link', 'fact_event__decision__candidate__trace__raw_message', 'fact_event__project')
        elif kind == 'task':
            qs = qs.select_related('source_message', 'candidate__trace__raw_message')
        objects = list(qs[(page - 1) * PAGE_SIZE:page * PAGE_SIZE])
        return {'metric': key, 'title': label, 'count': count, 'items': self.items(objects, kind),
                'page': page, 'next_page': page + 1 if page * PAGE_SIZE < count else None,
                'as_of': self.as_of.isoformat()}

    def items(self, objects, kind):
        """Batch source-link authorization. No raw text from an unauthorized node."""
        raw_ids = set()
        candidates = {}
        for obj in objects:
            candidate = None
            if kind == 'candidate':
                candidate = obj
            elif kind == 'question':
                candidate = obj.candidate
            elif kind == 'crm' and obj.fact_event_id and obj.fact_event.decision_id:
                candidate = obj.fact_event.decision.candidate
            elif kind == 'task' and obj.candidate_id:
                candidate = obj.candidate
            if candidate:
                candidates[obj.pk] = candidate
                raw_ids.add(candidate.trace.raw_message_id)
            if kind == 'task' and obj.source_message_id:
                raw_ids.add(obj.source_message_id)
        identity_facts = {}
        if kind == 'identity':
            for row in self.candidates.filter(object_identity_id__in=[obj.pk for obj in objects]).order_by('-id').values('id', 'object_identity_id', 'trace__raw_message_id'):
                identity_facts.setdefault(row['object_identity_id'], row)
                raw_ids.add(row['trace__raw_message_id'])
        records = {r.raw_message_id: r for r in records_scope(self.user).filter(raw_message_id__in=raw_ids)}
        allowed_question_targets = set(self.candidates.filter(
            pk__in=[obj.candidate_id for obj in objects]).values_list('pk', flat=True)) if kind == 'question' else set()
        allowed_projects = set(access.projects_for(self.user, include_archived=True).filter(
            pk__in=[obj.fact_event.project_id for obj in objects if kind == 'crm' and obj.fact_event_id]).values_list('id', flat=True)) if kind == 'crm' else set()
        result = []
        for obj in objects:
            candidate = candidates.get(obj.pk)
            source = records.get(candidate.trace.raw_message_id) if candidate else None
            if kind == 'task' and source is None:
                source = records.get(obj.source_message_id)
            item = {'id': obj.pk, 'label': '', 'description': '', 'at': _iso(obj.created_at),
                    'state': '', 'href': None, 'source_href': record_href(source) if source else None,
                    'crm_href': None}
            if kind == 'record':
                item.update(label=obj.raw_message.sender_name or 'Сообщение WhatsApp',
                            description=sanitize_body(obj.sanitized_body)[:500], at=_iso(obj.occurred_at),
                            state=obj.raw_message.processing_state, href=record_href(obj), source_href=record_href(obj))
            elif kind == 'thread':
                item.update(label=sanitize_body(obj.topic) or 'Тема не определена', description=sanitize_body(obj.summary)[:500], state=obj.state, href=f'/threads?thread={obj.pk}')
            elif kind == 'identity':
                item.update(label=sanitize_body(obj.canonical_name), description='Объект, выявленный в переписке.', state=obj.state)
                fact = identity_facts.get(obj.pk)
                if fact:
                    source = records.get(fact['trace__raw_message_id'])
                    item.update(href=f'/analytics?view=review&candidate={fact["id"]}', source_href=record_href(source) if source else None)
            elif kind == 'candidate':
                item.update(label={'project': 'Изменение сделки', 'commitment': 'Обязательство', 'payment': 'Платёж'}.get(obj.fact_type, 'Факт'), description=sanitize_body(getattr(obj, 'decision_explanation', '') or obj.review_reason or 'Проверьте основание и предложенные изменения.')[:500], state=obj.status, href=f'/analytics?view=review&candidate={obj.pk}')
            elif kind == 'question':
                recipient = obj.recipient.get_full_name() or obj.recipient.get_username()
                item.update(label='Уточнение суммы' if obj.kind == 'sale_amount' else 'Уточнение объекта/сделки', description=f'Получатель: {sanitize_body(recipient)}. Состояние: ' + {'queued': 'готовится', 'awaiting': 'ожидается ответ', 'answered': 'ответ получен, применение ожидается', 'unconfirmed': 'ответ не подтверждён', 'expired': 'срок ответа истёк'}.get(obj.state, obj.state), state=obj.state, href=f'/analytics?view=review&candidate={obj.candidate_id}' if obj.candidate_id in allowed_question_targets else None)
            elif kind == 'task':
                item.update(label=sanitize_body(obj.commitment_text)[:255], description='Срок: ' + (_iso(obj.deadline_at) or str(obj.deadline or 'не указан')) + '. Ответственный: ' + sanitize_body(obj.responsible_name or 'не указан'), state=obj.status, href='/analytics?view=tasks')
            elif kind == 'crm':
                name = obj.fact_event.project.name if obj.fact_event_id and obj.fact_event.project_id in allowed_projects else 'Изменение CRM'
                action = 'Создано' if obj.was_created else 'Обновлено' if obj.was_updated else 'Закрыто' if obj.was_completed else 'Результат без классификации действия'
                fields = ', '.join({'OPPORTUNITY': 'сумма', 'STAGE_ID': 'стадия', 'TITLE': 'название', 'DEADLINE': 'срок', 'RESPONSIBLE_ID': 'ответственный'}.get(k, k) for k in obj.patch)
                reason = {'crm_human_edit_conflict': 'В CRM есть изменения человека, требуется сверка.', 'crm_stage_mapping_missing': 'Не настроено соответствие стадий CRM.', 'crm_identity_unresolved': 'Не определена сделка для внесения изменений.', 'crm_readback_mismatch': 'Проверка CRM не подтвердила ожидаемые значения.', 'crm_disabled': 'Подключение к CRM отключено.'}.get(obj.error_code, 'Причина доступна в журнале CRM.' if obj.error_code else '')
                item.update(label=sanitize_body(name), description=f'{action}. Изменения: {fields or "значения уже актуальны"}. ' + reason, at=_iso(obj.updated_at), state=obj.state,
                            href=f'/analytics?view=review&candidate={candidate.pk}' if candidate else None,
                            crm_href='/crm-changes?' + urlencode({'object_type': obj.external_object_link.object_type, 'external_id': obj.external_object_link.external_id or '', 'delivery': obj.pk}))
            result.append(item)
        return result

    def summary(self):
        messages, dialogues, outcomes = self.messages(), self.dialogues(), self.outcomes()
        attention, sources = self.attention(), self.sources()
        count = sum(self.states.values())
        oldest_background = self.background.aggregate(at=Min('raw_message__received_at'))['at']
        oldest = min((at for at in (self.oldest_analysis, oldest_background) if at is not None), default=None)
        open_crm = self.crm.filter(state__in=OPEN_CRM).count()
        bad_crm = self.crm.exclude(state__in=GOOD_CRM + OPEN_CRM).count()
        questions = self.questions.count()
        pending = self.pending.count()
        technical = self.states.get('error', 0) + bad_crm
        unresolved = self.states.get('need_context', 0) + self.states.get('needs_review', 0) + questions + pending
        unresolved += self.registry['dialogues.context'][1].count() + self.registry['dialogues.unknown'][1].count()
        unresolved += self.media.count()
        unresolved += self.unscheduled_deals.count() + self.unscheduled_tasks.count()
        fresh = bool(sources) and all(s['status'] == 'fresh' for s in sources)
        ai = AISettings.get_active()
        integration = BitrixSettings.get_active()
        crm_enabled = ai.autonomous_enabled and ai.autonomous_crm_enabled and integration.is_active
        uncertain = self.uncertain_records.exists()
        active_history = WhatsAppHistoryRun.objects.filter(job__config__in=self.configs, state__in=['collecting', 'importing', 'analyzing']).exists()
        if technical:
            code, title, description = 'attention', 'Актуализация требует внимания', 'Есть ошибки разбора или неподтверждённые изменения CRM. Откройте причины ниже.'
        elif oldest or open_crm or active_history:
            code, title, description = 'processing', 'Актуализация выполняется', 'Часть сообщений или изменений CRM ещё обрабатывается.'
        elif unresolved:
            code, title, description = 'attention', 'Обработка требует уточнений', 'Ожидаются решения по фактам или ответы человека. Остальные результаты показаны отдельно.'
        elif not fresh or uncertain or not crm_enabled:
            code, title, description = 'unknown', 'Полнота актуализации не подтверждена', 'Проверьте свежесть источников и покрытие периода. Отсутствие очереди само по себе не подтверждает полноту.'
            if not crm_enabled:
                description += ' Автоматическое ведение CRM отключено или подключение не настроено.'
        elif count == 0:
            code, title, description = 'empty', 'За период новых сообщений нет', 'Источники проверены. Для анализа результатов выберите другой период.'
        else:
            code, title, description = 'complete', 'Актуализация завершена', 'Сообщения выбранного периода разобраны; все подготовленные изменения CRM подтверждены.'
        through = min((_date(s['covered_through'], 'covered_through') for s in sources if s['covered_through']), default=None) if fresh else None
        if code not in ('complete', 'empty'):
            through = None
        signals, comparison = self.signals(count, oldest, sources)
        good = self.crm.filter(state__in=GOOD_CRM)
        human_question = FactClarification.objects.filter(
            Q(candidate_id=OuterRef('fact_event__decision__candidate_id')) |
            Q(candidates__id=OuterRef('fact_event__decision__candidate_id')))
        sample = list(good.filter(fact_event__decision__candidate__trace__raw_message__received_at__lte=F('updated_at')).annotate(
            human_question=Exists(human_question)).order_by('-id').values_list(
                'updated_at', 'fact_event__decision__candidate__trace__raw_message__received_at',
                'fact_event__decision__actor_kind', 'fact_event__decision__candidate__reviewed_by_id', 'human_question')[:1000])
        delays = sorted((end - start).total_seconds() for end, start, actor, reviewer, question in sample)
        middle = len(delays) // 2
        median = (delays[middle] if len(delays) % 2 else (delays[middle - 1] + delays[middle]) / 2) if delays else None
        automatic = round(100 * sum(actor == 'system' and reviewer is None and not question
                                    for _, _, actor, reviewer, question in sample) / len(sample), 1) if sample else None
        activity = self.items(list(good.select_related('external_object_link', 'fact_event__decision__candidate__trace__raw_message', 'fact_event__project').order_by('-updated_at', '-id')[:8]), 'crm')
        teams = Team.objects.filter(Q(pk__in=access.team_ids(self.user)) | Q(pk__in=access.projects_for(self.user).values('team_id')))
        if self.user.is_superuser:
            teams = Team.objects.filter(is_active=True)
        chats = access.configs_for(self.user)
        if self.team_id:
            chats = chats.filter(team_id=self.team_id)
        return {'as_of': self.as_of.isoformat(), 'timezone': settings.TIME_ZONE,
                'from': _iso(self.start), 'to': self.end.isoformat(), 'coverage': 'scoped',
                'filters': {'teams': list(teams.order_by('name', 'id').values('id', 'name')), 'chats': list(chats.order_by('name', 'id').values('id', 'name', 'team_id'))},
                'status': {'code': code, 'title': title, 'description': description, 'processed_through': _iso(min(through, self.end)) if through else None, 'technical_issues': technical, 'human_questions': questions},
                'sources': sources, 'messages': messages, 'dialogues': dialogues, 'outcomes': outcomes,
                'attention': attention, 'signals': signals, 'comparison': comparison,
                'performance': {'oldest_pending_at': _iso(oldest), 'median_delivery_seconds': median, 'automatic_percent': automatic, 'sample_size': len(sample)},
                'activity': activity,
                'limitations': ['Показаны только доступные вам чаты, факты и CRM-объекты.', 'Обнаруженные факты и подтверждённые действия CRM — разные этапы. Группы внимания могут пересекаться.', 'Для локальных проектов доступно время последнего изменения. Полная история их обновлений пока недоступна.', 'Время до CRM и автоматизация рассчитаны по последним 1000 подтверждённым доставкам, связанным с сообщениями периода.']}

    def signals(self, count, oldest, sources):
        signals = []
        if oldest and oldest < self.as_of - timedelta(minutes=30):
            signals.append({'key': 'backlog', 'title': 'Сообщения долго ожидают обработки', 'description': 'Есть незавершённые сообщения, полученные более 30 минут назад. Проверьте, продвигается ли обработка.', 'metric': 'messages.delayed', 'severity': 'warning'})
        for source in sources:
            if source['status'] in ('stale', 'disconnected'):
                signals.append({'key': f'source:{source["id"]}', 'title': source['name'] + ': проверьте источник', 'description': source['description'], 'metric': f'source.{source["id"]}', 'severity': 'danger'})
        boundary = self.start or timezone.make_aware(datetime.combine(timezone.localdate(self.as_of), time.min))
        current_known = self.records.exclude(self.unknown_time)
        count = current_known.count()
        baseline = self.base.filter(occurred_at__gte=boundary - timedelta(days=28), occurred_at__lt=boundary, time_is_estimated=False, raw_message__sent_at_known=True)
        baseline_count = baseline.count()
        days = baseline.annotate(day=TruncDate('occurred_at')).values('day').distinct().count()
        if days < 7 or baseline_count < 100 or count < 20:
            return signals, 'Недостаточно данных для сравнения: нужны минимум 7 полных дней с сообщениями, 100 сообщений в базе сравнения и 20 в выбранном периоде.'
        previous_threads = DialogueThread.objects.filter(pk__in=ThreadMessage.objects.filter(raw_message_id__in=baseline.values('raw_message_id')).values('thread_id'), config_id__in=self.configs.values('id')).exclude(state='superseded')
        prev_total = previous_threads.count()
        current_threads = self.threads.filter(pk__in=ThreadMessage.objects.filter(
            raw_message_id__in=current_known.values('raw_message_id')).values('thread_id'))
        current_total = current_threads.count()
        expected = prev_total * count / baseline_count
        if current_total >= max(10, expected * 2):
            signals.append({'key': 'fragmentation', 'title': 'Необычно много обсуждений', 'description': f'{current_total * 100 / count:.1f} обсуждений на 100 сообщений; за предыдущие {days} дней с данными — {prev_total * 100 / baseline_count:.1f}. Порог: рост в 2 раза, минимум 10 обсуждений. Возможна смена характера переписки.', 'metric': 'dialogues.total', 'severity': 'warning'})
        prev_unknown = previous_threads.filter(state='unknown').count()
        cur_unknown = current_threads.filter(state='unknown').count()
        if current_total >= 10 and cur_unknown / current_total > max(0.2, 2 * prev_unknown / max(1, prev_total)):
            signals.append({'key': 'topics', 'title': 'Выросла доля неопределённых тем', 'description': f'{cur_unknown} из {current_total} обсуждений; ранее {prev_unknown} из {prev_total}. Порог: более 20% и рост доли более чем вдвое.', 'metric': 'dialogues.unknown', 'severity': 'warning'})
        historical_deals = cohort_deliveries(self.user, cohort_candidates(
            self.user, baseline.values('raw_message_id'), self.team_id)).filter(
                state__in=GOOD_CRM, external_object_link__object_type='deal', was_created=True,
            ).values('external_object_link_id').distinct().count()
        current_deals = cohort_deliveries(self.user, cohort_candidates(
            self.user, current_known.values('raw_message_id'), self.team_id)).filter(
                state__in=GOOD_CRM, was_created=True, external_object_link__object_type='deal',
            ).values('external_object_link_id').distinct().count()
        if current_deals >= max(5, 2 * historical_deals * count / baseline_count):
            signals.append({'key': 'new_deals', 'title': 'Необычно много новых сделок', 'description': f'Создано {current_deals}; база сравнения — {historical_deals} на {baseline_count} сообщений. Порог: не менее 5 и рост на сообщение в 2 раза. Проверьте новые сделки на возможные дубли.', 'metric': 'deal.created', 'severity': 'warning'})
        return signals, f'Сравнение с предыдущими 28 днями: {days} полных дней с данными, {baseline_count} сообщений. Показатели нормированы на число сообщений.'


class OverviewView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request):
        # All aggregates in a response observe one database snapshot.
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cursor.execute('SET LOCAL jit=off')
        return Response(Selection(request).summary())


class OverviewDetailsView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request):
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cursor.execute('SET LOCAL jit=off')
        selection = Selection(request, register_only=True)
        selection.messages()
        selection.dialogues()
        selection.outcomes()
        selection.attention()
        page = _ident(request.query_params, 'page') or 1
        if page > 100000:
            raise ValidationError({'error': 'Слишком большой номер страницы.'})
        return Response(selection.details(request.query_params.get('metric', ''), page))
