"""Scoped, durable reanalysis of current errors; historical attempts stay immutable."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery, Sum, Count
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
        processing_state__in=['clarification_reply', 'deleted', 'superseded', 'offtopic'])
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
    scheduled = []
    for target in targets[cursor:cursor + 50]:
        key = f"error-recovery-{operation.pk}"
        try:
            trace = schedule_reanalysis(target['id'], user, key, recovery_id=operation.pk,
                                       expected_trace_id=target['latest_trace_id'])
            disposition = 'scheduled' if trace.operation_key == f"reanalysis:{target['id']}:{key}" else 'already_processing'
            items.append({'raw_id': target['id'], 'trace_id': trace.pk, 'disposition': disposition})
            if disposition == 'scheduled':
                scheduled.append(trace)
        except (PermissionDenied, RawMessage.DoesNotExist):
            items.append({'raw_id': target['id'], 'disposition': 'unavailable'})
        except ProviderUnavailable as exc:
            items.append({'raw_id': target['id'], 'disposition':
                'changed' if str(exc) == 'reanalysis_source_changed' else
                'already_processing' if str(exc) == 'context_processing_in_progress' else 'unavailable'})
        cursor += 1
    from .live_batching import enqueue_recovery
    enqueue_recovery(scheduled, operation, user)
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
    pending_candidate_rows = list(FactCandidate.objects.filter(
        trace_id__in=[trace.pk for trace in traces.values()], status='pending'
    ).values_list('id', 'trace_id'))
    pending_candidate_ids = [candidate_id for candidate_id, _ in pending_candidate_rows]
    pending_candidates = {trace_id for _, trace_id in pending_candidate_rows}
    active_candidates = set(OutboxEvent.objects.filter(
        event_type__in=('decide_fact', 'crm_match'),
        state__in=['pending', 'enqueued', 'processing'],
        payload__candidate_id__in=pending_candidate_ids,
    ).values_list('payload__candidate_id', flat=True))
    active_candidate_traces = {trace_id for candidate_id, trace_id in pending_candidate_rows
                               if candidate_id in active_candidates}
    failed_raws = set(RawMessage.objects.filter(pk__in=raw_ids, processing_state='failed').values_list('pk', flat=True))
    active_raws = set()
    for anchor, batch in OutboxEvent.objects.filter(payload__error_recovery_id=operation.pk,
            state__in=['pending', 'enqueued', 'processing']).values_list('payload__raw_id', 'payload__batch_ids'):
        active_raws.update(batch or [anchor])
    trace_ids = [trace.pk for trace in traces.values()]
    active_raws.update(OutboxEvent.objects.filter(event_type='entity_reconcile',
        payload__trace_id__in=trace_ids, state__in=['pending', 'enqueued', 'processing'])
        .values_list('payload__raw_id', flat=True))
    for item in scheduled:
        trace = traces.get(item['raw_id'])
        if item['raw_id'] in active_raws or not trace or trace.pk < item['trace_id']:
            result['pending'] += 1
        elif trace.pk in active_candidate_traces:
            result['pending'] += 1
        elif trace.context_metadata.get('request_state') == 'needs_review' or trace.pk in pending_candidates:
            result['needs_review'] += 1
        elif trace.status == 'success':
            result['succeeded'] += 1
        elif trace.status == 'error' or item['raw_id'] in failed_raws:
            result['pending' if item['raw_id'] in active_raws else 'failed'] += 1
        else:
            result['pending' if item['raw_id'] in active_raws else 'failed'] += 1
    from .models import ProviderUsage
    roots = OutboxEvent.objects.filter(event_type='extract_message', payload__error_recovery_id=operation.pk)
    root_ids = list(roots.values_list('id', flat=True))
    root_ids.extend(OutboxEvent.objects.filter(event_type='entity_reconcile',
        payload__entity_operation_root_id__in=root_ids).values_list('id', flat=True))
    usage = ProviderUsage.objects.filter(Q(outbox_event_id__in=root_ids) | Q(outbox_event__payload__root_event_id__in=root_ids)).aggregate(
        calls=Count('id'), input_known=Count('input_tokens'), output_known=Count('output_tokens'),
        input_tokens=Sum('input_tokens'), output_tokens=Sum('output_tokens'))
    for field in ('input', 'output'):
        if usage.pop(f'{field}_known') != usage['calls']:
            usage[f'{field}_tokens'] = None
    status = ('running' if result['pending'] else 'completed') if operation.status == 'succeeded' else operation.status
    return {'operation_id': operation.pk, 'status': status, 'error_code': operation.error_code,
            'model': operation.request['model'], 'usage': usage, **result}


def finalize_review(event):
    """A rejected recovery output needs human review; provider failures remain failures."""
    if not event.payload.get('error_recovery_id') or event.state not in ('failed', 'done'):
        return
    from .ai_retries import EVIDENCE_ERRORS
    review_codes = EVIDENCE_ERRORS | {'invalid_schema', 'invalid_extraction_schema', 'extraction_json_parse',
        'fact_thread_missing', 'fact_thread_evidence_missing', 'fact_thread_evidence_conflict',
        'batch_classification_missing', 'thread_classification_invalid', 'target_classification_invalid',
        'target_classification_conflict', 'thread_completion_unproven', 'thread_sources_unavailable',
        'thread_source_conflict', 'thread_sources_duplicate', 'thread_keys_duplicate', 'thread_parent_invalid',
        'thread_revision_conflict', 'extraction_top_level_type', 'extraction_facts_missing',
        'extraction_facts_type', 'payment_evidence_contradiction', 'entity_comparison_invalid',
        'entity_fact_source_invalid', 'entity_target_coverage_invalid', 'entity_stage_budget_exhausted',
        'entity_stage_input_too_large', 'entity_projection_changed'}
    trace_ids = [row['trace_id'] for row in event.payload.get('trace_ids', [])] or [event.payload.get('trace_id')]
    for trace in MessageProcessingTrace.objects.filter(pk__in=trace_ids):
        _finalize_trace(event, trace, review_codes)


def _finalize_trace(event, trace, review_codes):
    from .models import RawMessage
    if not trace.raw_message_id or trace.context_metadata.get('request_state') == 'needs_review':
        return
    if event.state == 'done':
        if not FactCandidate.objects.filter(trace=trace, status='pending').exists():
            return
        if AISettings.get_active().autonomous_enabled:
            trace.result_summary = 'Повторный анализ завершён. Автономная проверка и сопоставление предложений продолжаются.'
            trace.save(update_fields=['result_summary'])
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
