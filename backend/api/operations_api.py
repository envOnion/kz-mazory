"""Scoped operations, metrics and CRM history for the messenger shell."""
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import BooleanField, Case, CharField, Count, Exists, F, Func, OuterRef, Q, Value, When, Window
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, RowNumber, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .communication import sanitize_body, sanitize_data
from .messenger_api import aggregate_analysis_axes, decode_cursor, encode_cursor, page_size, records_scope, record_href
from .models import (BitrixDealChangeLog, BitrixSettings, CommunicationRecord,
    CrmCatalogSync, CrmDelivery, Commitment, MessageProcessingTrace, NotificationDelivery,
    SourceWorkItem, Team, Project)
from .operation_scopes import crm_scope, outbox_scope, outbox_team_scope

CRM_STATE_LABELS = {'pending': 'Запланировано', 'queued': 'В очереди', 'sending': 'Отправляется',
    'accepted': 'Запрос принят', 'confirmed': 'Подтверждено чтением', 'failed': 'Ошибка',
    'unknown': 'Результат неизвестен', 'unavailable': 'Недоступно для проверки',
    'partial': 'Частично выполнено', 'superseded': 'Заменено', 'blocked': 'Заблокировано'}
CRM_STATES = {'delivered': 'confirmed', 'error': 'failed', 'success': 'accepted'}
DELIVERY_TYPES = ('notification', 'otp', 'notification_reconcile', 'delivery_ack', 'waha_control')
HISTORY_TYPES = ('history_import', 'thread_backfill')
CRM_TYPES = ('crm_sync', 'autonomous_crm', 'crm_import', 'crm_catalog', 'crm_match', 'decide_fact', 'clarification_reply', 'price_question')
AI_TYPES = ('extract_message', 'route_message', 'extract_thread', 'index_message', 'operation', 'attachment', 'whatsapp_artifact')


def _team(request):
    value = request.query_params.get('team')
    if not value:
        return None
    if not value.isdecimal() or len(value)>18 or int(value)<1:
        raise ValidationError({'error':'Укажите положительный целочисленный ID команды.'})
    return int(value)


def _teams(user):
    qs = Team.objects.all()
    if not user.is_superuser:
        qs = qs.filter(Q(id__in=access.team_ids(user))|Q(id__in=access.projects_for(user,include_client=True).values('team_id')))
    return list(qs.distinct().order_by('name','id').values('id','name'))


def _period(request):
    values = []
    for name in ('from', 'to'):
        raw = request.query_params.get(name)
        try:
            value = timezone.datetime.fromisoformat(raw) if raw else None
            if value and timezone.is_naive(value):
                value = timezone.make_aware(value, timezone.get_current_timezone())
        except (ValueError, TypeError):
            raise ValidationError({'error': 'Укажите дату в формате ISO 8601.'}) from None
        values.append(value)
    if all(values) and values[0] >= values[1]:
        raise ValidationError({'error': 'Начало периода должно предшествовать окончанию.'})
    return values


def _window(qs, field, period):
    start, end = period
    if start:
        qs = qs.filter(**{field + '__gte': start})
    if end:
        qs = qs.filter(**{field + '__lt': end})
    return qs


def _latest_crm(qs):
    return qs.annotate(_rank=Window(expression=RowNumber(),
        partition_by=[F('external_object_link_id'), F('target_version')],
        order_by=[F('updated_at').desc(), F('id').desc()])).filter(_rank=1)


def bitrix_object_url(kind, external_id, config, assignee=None):
    # Derive only the public origin; never expose the webhook path/token.
    cfg = config
    try:
        parsed = urlsplit(cfg.webhook_url if cfg else '')
    except ValueError:
        return None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return None
    if not external_id or not external_id.isdecimal():
        return None
    tail = {'deal': 'crm/deal/details', 'company': 'crm/company/details',
            'contact': 'crm/contact/details', 'task': 'company/personal/user/0/tasks/task/view'}.get(kind)
    if kind == 'task':
        manager = assignee
        if not isinstance(manager,str) or not manager.isdecimal():
            return None
        return f'https://{parsed.hostname}/company/personal/user/{manager}/tasks/task/view/{external_id}/'
    return f'https://{parsed.hostname}/{tail}/{external_id}/' if tail else None


def _cursor_filter(qs, cursor, source, field='updated_at'):
    if not cursor:
        return qs
    if not isinstance(cursor, dict):
        raise ValidationError({'error': 'Некорректный курсор.'})
    moment, ident, anchor_source = cursor.get('t'), cursor.get('i'), cursor.get('s')
    if 't' not in cursor or (moment is not None and not isinstance(moment, str)) or type(ident) is not int or not isinstance(anchor_source, str):
        raise ValidationError({'error': 'Некорректный курсор.'})
    if moment is None:
        condition = Q(**{field + '__isnull': True, 'id__lt': ident})
        if source < anchor_source:
            condition |= Q(**{field + '__isnull': True, 'id': ident})
        return qs.filter(condition)
    try:
        parsed = parse_datetime(moment)
    except ValueError:
        parsed = None
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationError({'error': 'Некорректный курсор.'})
    condition = (Q(**{field + '__lt': parsed}) | Q(**{field: parsed, 'id__lt': ident})
                 | Q(**{field + '__isnull': True}))
    if source < anchor_source:
        condition |= Q(**{field: parsed, 'id': ident})
    return qs.filter(condition)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _page(items, limit):
    items.sort(key=lambda row: (row['updated_at'] is not None, row['updated_at'] or '', row['id'], row['source']), reverse=True)
    more = len(items) > limit
    items = items[:limit]
    last = items[-1] if items else None
    return {'items': items, 'next_cursor': encode_cursor({'t': last['updated_at'], 'i': last['id'], 's': last['source']}) if more else None,
            'as_of': timezone.now().isoformat(), 'coverage': 'scoped'}


class AnalyticsOperationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = _period(request)
        scope = records_scope(request.user)
        team = request.query_params.get('team')
        if team:
            if not team.isdecimal():
                raise ValidationError({'error': 'Укажите целочисленный ID команды.'})
            scope = scope.filter(team_id=int(team))
        inbound = _window(scope.filter(direction='inbound', purpose='source'), 'occurred_at', period)
        # The status CTE is shared with Messenger filters. Aggregate the complete
        # scoped cohort in PostgreSQL; do not transfer every inbound row to Python.
        states, attention_count = aggregate_analysis_axes(inbound)
        crm = crm_scope(request.user)
        events = outbox_scope(request.user)
        if team:
            crm = crm.filter(external_object_link__team_id=int(team))
            events = outbox_team_scope(events,int(team))
        crm = _window(crm, 'updated_at', period)
        latest = _latest_crm(crm)
        crm_states = {}
        for state in latest.values_list('state', flat=True):
            code = CRM_STATES.get(state, state)
            crm_states[code] = crm_states.get(code, 0) + 1
        deliveries = NotificationDelivery.objects.filter(notification_id__in=scope.values('notification_id'))
        latest_deliveries = deliveries.annotate(_rank=Window(expression=RowNumber(),
            partition_by=[F('notification_id')], order_by=[F('attempt_no').desc(), F('id').desc()])).filter(_rank=1)
        traces = MessageProcessingTrace.objects.filter(raw_message_id__in=inbound.values('raw_message_id'))
        latest_events = _window(events, 'updated_at', period)
        current_errors = {'analysis': states.get('error', 0),
            'delivery': latest_deliveries.filter(state__in=['failed','unknown']).count(),
            'outbox': latest_events.filter(state__in=['failed','unknown']).count(),
            'crm': latest.filter(state__in=['failed','unknown','blocked','partial']).count()}
        failed_attempts = {'analysis_traces': traces.filter(status='error').count(),
            'delivery_attempts': _window(deliveries, 'created_at', period).filter(state='failed').count(),
            'outbox_retries': latest_events.filter(attempt_count__gt=1).count()}
        daily = list(_window(scope.filter(direction='inbound', purpose='source',
            occurred_at__gte=period[0] or timezone.now()-timedelta(days=30)), 'occurred_at', period)
            .annotate(day=TruncDate('occurred_at')).values('day').annotate(n=Count('id')).order_by('-day')[:60])
        daily.reverse()
        catalog = None
        if team and (request.user.is_superuser or int(team) in access.team_ids(request.user)):
            sync = CrmCatalogSync.objects.filter(team_id=int(team)).first()
            if sync:
                catalog = {'last_success_at': sync.last_success_at, 'imported_count': sync.imported_count,
                    'state': sync.state, 'coverage': 'complete' if sync.state == 'done' and sync.last_success_at else 'partial'}
        return Response({'as_of': timezone.now().isoformat(), 'timezone': settings.TIME_ZONE, 'coverage': 'scoped',
            'messages': {'inbound_total': inbound.count(), 'by_state': states, 'attention': attention_count,
                'outbound_by_purpose': dict(_window(scope.filter(direction='outbound'), 'occurred_at', period).values_list('purpose').annotate(n=Count('id')))},
            'crm': {'operations_by_state': crm_states, 'logical_operations': sum(crm_states.values()), 'attempts_total': crm.count()},
            'errors': {'current': current_errors, 'failed_attempts': failed_attempts},
            'dynamics': [{'day': row['day'].isoformat() if row['day'] else None, 'messages': row['n']} for row in daily], 'catalog': catalog})


class ProcessingJobsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queue = request.query_params.get('queue', 'all')
        if queue not in ('all','ai','delivery','history','crm'):
            raise ValidationError({'error': 'Неизвестная очередь.'})
        limit, cursor, now = page_size(request), decode_cursor(request.query_params.get('cursor')), timezone.now()
        state = request.query_params.get('state', '')
        team = _team(request)
        source = request.query_params.get('source','')
        if source not in ('','source_work','outbox'):
            raise ValidationError({'error':'Неизвестный источник задания.'})
        period = _period(request)
        items = []
        if queue in ('all','ai') and source != 'outbox':
            qs = SourceWorkItem.objects.filter(raw_message__in=access.messages_for(request.user))
            if team:
                qs = qs.filter(Q(raw_message__team_id=team)|Q(raw_message__team__isnull=True,raw_message__config__team_id=team))
            qs = _cursor_filter(_window(qs, 'updated_at', period), cursor, 'source_work')
            if state == 'stale':
                qs = qs.filter(lease_until__lt=now).exclude(state__in=['pending','done','completed','error','failed'])
            elif state:
                qs = qs.filter(state=state)
            rows = list(qs.only('id', 'raw_message_id', 'processing_version', 'error_code',
                                'lease_until', 'state', 'created_at', 'updated_at')
                        .order_by(F('updated_at').desc(nulls_last=True),'-id')[:limit+1])
            raw_ids = {row.raw_message_id for row in rows if row.raw_message_id}
            records = records_scope(request.user).filter(raw_message_id__in=raw_ids).only(
                'id', 'raw_message_id', 'session_name', 'chat_id', 'category', 'team_id'
            ).order_by('id')
            records_by_raw_id = {}
            for record in records:
                records_by_raw_id.setdefault(record.raw_message_id, record)
            for row in rows:
                stale = row.lease_until and row.lease_until < now and row.state not in ('pending','done','completed','error','failed')
                record = records_by_raw_id.get(row.raw_message_id)
                items.append({'queue':'ai', 'source':'source_work', 'id':row.pk, 'type':'source_work',
                    'state':'stale' if stale else row.state, 'raw_message_id':row.raw_message_id,
                    'processing_version':row.processing_version, 'error_code':sanitize_body(row.error_code) or None,
                    'lease_until':row.lease_until.isoformat() if row.lease_until else None,
                    'can_retry':False, 'record_href':record_href(record) if record else None,
                    'created_at':_iso(row.created_at), 'updated_at':_iso(row.updated_at)})
        if source != 'source_work':
            types = {'all':DELIVERY_TYPES+HISTORY_TYPES+CRM_TYPES+AI_TYPES, 'ai':AI_TYPES,
                     'delivery':DELIVERY_TYPES, 'history':HISTORY_TYPES, 'crm':CRM_TYPES}[queue]
            qs = outbox_scope(request.user).filter(event_type__in=types)
            if team:
                qs = outbox_team_scope(qs,team)
            qs = _cursor_filter(_window(qs, 'updated_at', period), cursor, 'outbox')
            if state == 'stale':
                qs = qs.filter(state__in=['enqueued','processing'], lease_until__lt=now)
            elif state:
                qs = qs.filter(state=state)
            # Filter duplicate logical AI jobs before slicing so skipped rows cannot
            # shorten a page or push its cursor past unseen records.
            source_work = SourceWorkItem.objects.annotate(
                _raw_message_id_text=Cast('raw_message_id', CharField()),
            ).filter(
                _raw_message_id_text=KeyTextTransform('raw_id', OuterRef('payload')),
            )
            qs = qs.annotate(
                _payload_raw_id_type=Func(
                    KeyTransform('raw_id', F('payload')),
                    function='jsonb_typeof', output_field=CharField(),
                ),
            ).annotate(
                _has_source_work=Case(
                    When(_payload_raw_id_type='number', then=Exists(source_work)),
                    default=Value(False), output_field=BooleanField(),
                ),
            ).exclude(event_type__in=AI_TYPES, _has_source_work=True)
            rows = qs.order_by(F('updated_at').desc(nulls_last=True),'-id').values(
                'id', 'event_type', 'state', 'attempt_count', 'error_code',
                'next_attempt_at', 'lease_until', 'created_at', 'updated_at',
            )[:limit+1]
            integration_allowed = access.integration_allowed(request.user)
            for row in rows:
                tag = 'delivery' if row['event_type'] in DELIVERY_TYPES else 'history' if row['event_type'] in HISTORY_TYPES else 'ai' if row['event_type'] in AI_TYPES else 'crm'
                items.append({'queue':tag, 'source':'outbox', 'id':row['id'], 'type':row['event_type'], 'state':row['state'],
                    'attempt_count':row['attempt_count'], 'error_code':sanitize_body(row['error_code']) or None,
                    'next_attempt_at':row['next_attempt_at'].isoformat() if row['next_attempt_at'] else None,
                    'lease_until':row['lease_until'].isoformat() if row['lease_until'] else None,
                    'can_retry':integration_allowed and row['state'] in ('failed','unknown') and tag != 'ai' and row['event_type'] not in ('otp','waha_control'),
                    'requires_delivery_confirmation':row['event_type'] == 'notification' and row['state'] == 'unknown',
                    'created_at':_iso(row['created_at']), 'updated_at':_iso(row['updated_at'])})
        return Response({**_page(items, limit),'teams':_teams(request.user)})


class CrmChangesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit, cursor, period = page_size(request), decode_cursor(request.query_params.get('cursor')), _period(request)
        qs = crm_scope(request.user).select_related(
            'external_object_link', 'fact_event__decision__candidate__trace',
            'fact_event__decision__candidate__reviewed_by', 'outbox_event',
        ).only(
            'id', 'external_object_link_id', 'fact_event_id', 'outbox_event_id', 'state',
            'error_code', 'patch', 'created_at', 'updated_at',
            'external_object_link__id', 'external_object_link__team_id',
            'external_object_link__object_type', 'external_object_link__local_id',
            'external_object_link__external_id',
            'fact_event__id', 'fact_event__event_type', 'fact_event__decision_id',
            'fact_event__decision__id', 'fact_event__decision__actor_kind',
            'fact_event__decision__candidate_id', 'fact_event__decision__candidate__id',
            'fact_event__decision__candidate__trace_id',
            'fact_event__decision__candidate__reviewed_by_id',
            'fact_event__decision__candidate__trace__id',
            'fact_event__decision__candidate__trace__raw_message_id',
            'fact_event__decision__candidate__reviewed_by__id',
            'fact_event__decision__candidate__reviewed_by__first_name',
            'fact_event__decision__candidate__reviewed_by__last_name',
            'outbox_event__id',
        ).annotate(_crm_baseline=F('outbox_event__payload__crm_baseline'))
        logs = BitrixDealChangeLog.objects.annotate(
            _journal_fields=F('payload__fields'),
            _journal_proof=F('response_data__proof'),
            _journal_team_id=F('project__team_id'),
        ).only(
            'id', 'project_id', 'source_project_id', 'object_type', 'external_id',
            'bitrix_deal_id', 'action', 'method', 'status', 'error_message',
            'reconstructed', 'created_at', 'updated_at',
        )
        if not request.user.is_superuser:
            projects = access.projects_for(request.user)
            logs = logs.filter(Q(project__in=projects) | Q(source_project_id__in=projects.values('id')))
        receipt_id = request.query_params.get('receipt_id')
        if receipt_id:
            if not receipt_id.isdecimal():
                raise ValidationError({'error':'Укажите целочисленный ID записи журнала.'})
            receipt = logs.filter(pk=int(receipt_id)).first()
            qs = qs.filter(pk=receipt.delivery_id) if receipt and receipt.delivery_id else qs.none()
            logs = logs.filter(pk=receipt.pk) if receipt else logs.none()
        # Delivery-linked receipts are attempts within one logical operation.
        logs = logs.exclude(delivery_id__in=CrmDelivery.objects.values('id'))
        team = _team(request)
        if team:
            qs = qs.filter(external_object_link__team_id=team)
            logs = logs.filter(Q(project__team_id=team)|Q(source_project_id__in=Project.objects.filter(team_id=team).values('id')))
        source = request.query_params.get('source','')
        if source not in ('','delivery','journal'):
            raise ValidationError({'error':'Неизвестный источник CRM.'})
        if source == 'delivery': logs = logs.none()
        if source == 'journal': qs = qs.none()
        external_id = request.query_params.get('external_id','').strip()
        if len(external_id)>64:
            raise ValidationError({'error':'Внешний ID должен содержать до 64 символов.'})
        if external_id:
            qs = qs.filter(external_object_link__external_id=external_id)
            logs = logs.filter(Q(external_id=external_id)|Q(external_id='',bitrix_deal_id=external_id))
        action = request.query_params.get('action','')
        if action:
            if action not in ('add','update','delete','complete','defer'):
                raise ValidationError({'error':'Неизвестное действие CRM.'})
            method = r'\.'+action+r'(\.json)?$'
            qs = qs.filter(id__in=BitrixDealChangeLog.objects.filter(method__regex=method).values('delivery_id'))
            legacy = {'add':['create','add'],'update':['update','change']}.get(action,[action])
            logs = logs.filter(Q(method__regex=method)|Q(method='',action__in=legacy))
        object_type = request.query_params.get('object_type')
        if object_type:
            qs = qs.filter(external_object_link__object_type=object_type)
            logs = logs.filter(object_type=object_type)
        state = request.query_params.get('state')
        if state:
            qs = qs.filter(state__in=[s for s in (state, 'delivered' if state == 'confirmed' else state) if s])
            logs = logs.filter(status__in=[state, 'error' if state == 'failed' else 'success' if state == 'accepted' else state])
        qs = _cursor_filter(_window(qs, 'updated_at', period), cursor, 'delivery')
        logs = _cursor_filter(_window(logs, 'updated_at', period), cursor, 'journal')
        items = []
        deliveries = list(qs.order_by(F('updated_at').desc(nulls_last=True),'-id')[:limit+1])
        config = BitrixSettings.objects.filter(is_active=True).only('id', 'webhook_url').first()
        task_owners = dict(Commitment.objects.filter(pk__in=[row.external_object_link.local_id for row in deliveries
            if row.external_object_link.object_type == 'task']).values_list('id','manager__bitrix_user_id'))
        raw_ids = [row.fact_event.decision.candidate.trace.raw_message_id for row in deliveries
            if row.fact_event_id and row.fact_event.decision_id and row.fact_event.decision.candidate.trace_id]
        records = {record.raw_message_id:record for record in records_scope(request.user).filter(
            raw_message_id__in=raw_ids
        ).only('id', 'raw_message_id', 'session_name', 'chat_id', 'category', 'team_id')}
        receipts, methods, reconstructed = {}, {}, set()
        for receipt in BitrixDealChangeLog.objects.filter(
            delivery_id__in=[row.pk for row in deliveries]
        ).only('id','delivery_id','method','reconstructed').annotate(
            _receipt_proof=F('response_data__proof')
        ).order_by('-id'):
            receipts.setdefault(receipt.delivery_id,receipt)
            if receipt.method:
                methods.setdefault(receipt.delivery_id,set()).add(receipt.method)
            if receipt.reconstructed:
                reconstructed.add(receipt.delivery_id)
        for row in deliveries:
            link = row.external_object_link
            # Stored outbound patches prove intent, while delivered proves readback.
            record = None
            # Traverse actual source FKs without relying on reverse-name guesses.
            candidate = row.fact_event.decision.candidate if row.fact_event_id and row.fact_event.decision_id else None
            if candidate and candidate.trace_id:
                record = records.get(candidate.trace.raw_message_id)
            baseline = row._crm_baseline
            receipt = receipts.get(row.pk)
            proof = receipt._receipt_proof if receipt else None
            observed_owner = proof.get('responsibleId') if isinstance(proof,dict) else None
            owner = observed_owner if isinstance(observed_owner,str) and observed_owner.isdecimal() else task_owners.get(link.local_id)
            actor = row.fact_event.decision.actor_kind if row.fact_event_id and row.fact_event.decision_id else None
            reviewer = candidate.reviewed_by if candidate and candidate.reviewed_by_id else None
            items.append({'id':row.pk, 'source':'delivery', 'object_type':link.object_type,
                'team_id':link.team_id, 'actor_kind':actor,
                'actor_label':(reviewer.get_full_name() or f'Пользователь #{reviewer.pk}') if actor=='human' and reviewer else
                    'Пользователь (имя не сохранено)' if actor=='human' else 'Система' if actor=='system' else 'Автор не сохранён',
                'external_id':link.external_id, 'external_url':bitrix_object_url(link.object_type, link.external_id, config,owner),
                'local_id':link.local_id, 'action':row.fact_event.event_type if row.fact_event_id else None, 'method':receipt.method if receipt else None,
                'methods':sorted(methods.get(row.pk,set())),
                'reconstructed':row.pk in reconstructed,
                'state':CRM_STATES.get(row.state, row.state), 'error_code':sanitize_body(row.error_code) or None,
                'before':baseline if isinstance(baseline, dict) else None, 'after':row.patch or None,
                'fact_event_id':row.fact_event_id, 'outbox_event_id':row.outbox_event_id,
                'record_href':record_href(record) if record else None,
                'created_at':_iso(row.created_at), 'updated_at':_iso(row.updated_at)})
        for row in logs.order_by(F('updated_at').desc(nulls_last=True),'-id')[:limit+1]:
            fields = row._journal_fields if isinstance(row._journal_fields, dict) else {}
            proof = row._journal_proof if isinstance(row._journal_proof, dict) else {}
            items.append({'id':row.pk, 'source':'journal', 'object_type':row.object_type,
                'team_id':row._journal_team_id if row.project_id else None, 'actor_kind':None,'actor_label':'Автор не сохранён',
                'external_id':row.external_id or row.bitrix_deal_id or None,
                'external_url':bitrix_object_url(row.object_type, row.external_id or row.bitrix_deal_id, config,assignee=proof.get('responsibleId')),
                'local_id':row.project_id, 'action':row.action, 'method':row.method, 'state':CRM_STATES.get(row.status,row.status),
                'error_code':sanitize_body(row.error_message) or None, 'before':None,
                'after':fields.get('fields'), 'fact_event_id':None, 'outbox_event_id':None,
                'reconstructed':row.reconstructed, 'record_href':None,
                'created_at':_iso(row.created_at), 'updated_at':_iso(row.updated_at)})
        # These URLs are constructed above from a public origin and numeric
        # object/assignee IDs, never copied from payloads. The generic opaque
        # token regex also matches long URL paths; keep this validated field.
        public_urls = {(row['source'],row['id']):row['external_url'] for row in items}
        page = _page(sanitize_data(items),limit)
        for row in page['items']:
            row['external_url'] = public_urls[(row['source'],row['id'])]
        page['state_labels'] = CRM_STATE_LABELS
        page['teams'] = _teams(request.user)
        return Response(page)
