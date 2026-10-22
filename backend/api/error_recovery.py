"""Scoped, durable reanalysis of current errors; historical attempts stay immutable."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from . import access
from .models import AISettings, AsyncOperation, AuditEvent, FactCandidate, MessageProcessingTrace, OutboxEvent
from .providers import ProviderUnavailable
from .security import Conflict


def candidates(user, scope):
    from .messenger_api import records_scope
    records = records_scope(user)
    for field in ('session_name', 'chat_id', 'category'):
        if field in scope:
            records = records.filter(**{field: scope[field]})
    if 'team' in scope:
        records = records.filter(team_id=scope['team'])
    raws = access.messages_for(user).filter(pk__in=records.values('raw_message_id')).exclude(
        processing_state__in=['clarification_reply', 'deleted', 'superseded'])
    if not user.is_superuser:
        teams = access.team_ids(user, ['team_lead'])
        raws = raws.filter(Q(config__team_id__in=teams) | Q(config__isnull=True, team_id__in=teams))
    latest = MessageProcessingTrace.objects.filter(raw_message_id=OuterRef('pk')).order_by('-id')
    return raws.annotate(
        latest_trace_id=Subquery(latest.values('id')[:1]),
        latest_trace_status=Subquery(latest.values('status')[:1]),
    ).filter(Q(processing_state='failed') | Q(latest_trace_status='error')).order_by('pk')


def checked_configuration():
    from .ai_connection_checks import config_fingerprint
    cfg = AISettings.get_active()
    if not cfg.pk or not cfg.is_active:
        raise ProviderUnavailable('ai_not_configured')
    fingerprint = config_fingerprint(cfg, 'chat')
    if not AsyncOperation.objects.filter(operation_type='ai_connection_check', status='succeeded',
            request__config_id=cfg.pk, request__role='chat', request__fingerprint=fingerprint).exists():
        raise ProviderUnavailable('recovery_connection_check_required')
    return cfg, fingerprint


@transaction.atomic
def start(user, scope, request_key):
    if not user.is_superuser and not access.memberships(user).filter(role='team_lead').exists():
        raise PermissionDenied('Недостаточно прав для повторного анализа.')
    previous = AsyncOperation.objects.filter(requested_by=user, idempotency_key=request_key).first()
    if previous:
        if previous.access_fingerprint != access.fingerprint(user):
            raise PermissionDenied('Доступ к операции изменился.')
        if previous.operation_type != 'error_reanalysis' or previous.request.get('scope') != scope:
            raise Conflict('Ключ запроса уже использован для другой операции.')
        return previous
    cfg, fingerprint = checked_configuration()
    targets = list(candidates(user, scope).values('id', 'latest_trace_id'))
    operation, created = AsyncOperation.objects.get_or_create(requested_by=user, idempotency_key=request_key,
        defaults={'operation_type': 'error_reanalysis', 'access_fingerprint': access.fingerprint(user),
        'expires_at': timezone.now() + timedelta(days=1),
        'request': {'scope': scope, 'targets': targets, 'config_id': cfg.pk, 'fingerprint': fingerprint,
                    'model': cfg.chat_model_name}, 'result': {'items': [], 'cursor': 0}})
    if not created:
        if operation.access_fingerprint != access.fingerprint(user):
            raise PermissionDenied('Доступ к операции изменился.')
        if operation.operation_type != 'error_reanalysis' or operation.request.get('scope') != scope:
            raise Conflict('Ключ запроса уже использован для другой операции.')
        return operation
    OutboxEvent.objects.create(event_type='reanalyze_errors',
        deduplication_key=f'error-reanalysis:{operation.pk}', payload={'operation_id': operation.pk})
    AuditEvent.objects.create(actor=user, target_type='AsyncOperation', target_id=operation.pk,
        action='error_recovery.requested', before_after={'found': len(targets), 'config_id': cfg.pk})
    return operation


def validate_route(operation):
    from .ai_connection_checks import config_fingerprint
    cfg = AISettings.get_active()
    if cfg.pk != operation.request['config_id'] or not cfg.is_active or config_fingerprint(cfg, 'chat') != operation.request['fingerprint']:
        raise ProviderUnavailable('recovery_configuration_changed')
    return cfg


@transaction.atomic
def process(payload):
    from .processing_attempts import schedule_reanalysis
    from .models import RawMessage
    operation = AsyncOperation.objects.select_for_update().get(pk=payload['operation_id'], operation_type='error_reanalysis')
    if operation.status not in ('queued', 'running'):
        return
    user = get_user_model().objects.get(pk=operation.requested_by_id)
    if operation.expires_at <= timezone.now() or not user.is_active or operation.access_fingerprint != access.fingerprint(user):
        operation.status, operation.error_code = 'failed', 'reanalysis_access_revoked'
        operation.save(update_fields=['status', 'error_code', 'updated_at'])
        return
    try:
        validate_route(operation)
    except ProviderUnavailable as exc:
        operation.status, operation.error_code = 'failed', str(exc)
        operation.save(update_fields=['status', 'error_code', 'updated_at'])
        return
    items = operation.result.get('items', [])
    cursor = operation.result.get('cursor', 0)
    targets = operation.request['targets']
    for target in targets[cursor:cursor + 50]:
        key = f"error-recovery-{operation.pk}"
        try:
            trace = schedule_reanalysis(target['id'], user, key, recovery_id=operation.pk,
                                       expected_trace_id=target['latest_trace_id'])
            disposition = 'scheduled' if trace.operation_key == f"reanalysis:{target['id']}:{key}" else 'already_processing'
            items.append({'raw_id': target['id'], 'trace_id': trace.pk, 'disposition': disposition})
        except (PermissionDenied, RawMessage.DoesNotExist):
            items.append({'raw_id': target['id'], 'disposition': 'unavailable'})
        except ProviderUnavailable as exc:
            items.append({'raw_id': target['id'], 'disposition':
                'changed' if str(exc) == 'reanalysis_source_changed' else
                'already_processing' if str(exc) == 'context_processing_in_progress' else 'unavailable'})
        cursor += 1
    operation.result = {'items': items, 'cursor': cursor}
    operation.status = 'succeeded' if cursor >= len(targets) else 'running'
    operation.save(update_fields=['status', 'result', 'updated_at'])
    return {'history_continuation': True} if cursor < len(targets) else None


def progress(operation):
    from .models import RawMessage
    items = operation.result.get('items', [])
    result = {'found': len(operation.request['targets']), 'scheduled': 0, 'already_processing': 0,
              'changed': 0, 'unavailable': 0, 'pending': 0, 'succeeded': 0, 'needs_review': 0, 'failed': 0}
    scheduled = [item for item in items if item['disposition'] == 'scheduled']
    for item in items:
        result[item['disposition']] += 1
    raw_ids = [item['raw_id'] for item in scheduled]
    latest_ids = MessageProcessingTrace.objects.filter(raw_message_id__in=raw_ids).order_by('raw_message_id', '-id').distinct('raw_message_id').values('id')
    traces = {trace.raw_message_id: trace for trace in MessageProcessingTrace.objects.filter(pk__in=Subquery(latest_ids))}
    pending_candidates = set(FactCandidate.objects.filter(trace_id__in=[trace.pk for trace in traces.values()], status='pending').values_list('trace_id', flat=True))
    failed_raws = set(RawMessage.objects.filter(pk__in=raw_ids, processing_state='failed').values_list('pk', flat=True))
    active_raws = set(OutboxEvent.objects.filter(payload__error_recovery_id=operation.pk,
        state__in=['pending', 'enqueued', 'processing']).values_list('payload__raw_id', flat=True))
    for item in scheduled:
        trace = traces.get(item['raw_id'])
        if not trace or trace.pk < item['trace_id']:
            result['pending'] += 1
        elif (trace.context_metadata.get('request_state') == 'needs_review' and trace.status != 'success') or trace.pk in pending_candidates:
            result['needs_review'] += 1
        elif trace.status == 'success':
            result['succeeded'] += 1
        elif trace.status == 'error' or item['raw_id'] in failed_raws:
            result['pending' if item['raw_id'] in active_raws else 'failed'] += 1
        else:
            result['pending' if item['raw_id'] in active_raws else 'failed'] += 1
    return {'operation_id': operation.pk, 'status': operation.status, 'error_code': operation.error_code,
            'model': operation.request['model'], **result}


def finalize_review(event):
    """A rejected recovery output needs human review; provider failures remain failures."""
    if not event.payload.get('error_recovery_id') or event.state not in ('failed', 'done'):
        return
    from .ai_retries import EVIDENCE_ERRORS
    from .models import AuditEvent, RawMessage
    review_codes = EVIDENCE_ERRORS | {'invalid_schema', 'invalid_extraction_schema', 'extraction_json_parse',
        'fact_thread_missing', 'fact_thread_evidence_missing', 'fact_thread_evidence_conflict',
        'batch_classification_missing', 'thread_classification_invalid', 'target_classification_invalid'}
    trace = MessageProcessingTrace.objects.filter(pk=event.payload.get('trace_id')).first()
    if not trace or trace.context_metadata.get('request_state') == 'needs_review':
        return
    if event.state == 'done':
        if not FactCandidate.objects.filter(trace=trace, status='pending').exists():
            return
        trace.result_summary = 'Повторный анализ завершён. Новые предложения ожидают проверки перед записью в CRM.'
    else:
        if event.error_code not in review_codes or trace.status != 'error':
            return
        trace.status = 'warning'
        trace.result_summary = 'Повторный анализ не подтвердил данные. Требуется проверка человеком; новые факты не приняты.'
    trace.context_metadata['request_state'] = 'needs_review'
    trace.save(update_fields=['status', 'context_metadata', 'result_summary', 'updated_at'])
    if not trace.raw_message.traces.filter(id__gt=trace.pk).exists():
        RawMessage.objects.filter(pk=trace.raw_message_id).update(processing_state='needs_review')
    AuditEvent.objects.create(target_type='MessageProcessingTrace', target_id=trace.pk,
        action='error_recovery.needs_review', before_after={'error_code': trace.error_code})
