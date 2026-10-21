"""Durable retries for background message AI work; no sleeping worker threads."""

import copy
import json
import random
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import MessageProcessingTrace, OutboxEvent, RawMessage
from .providers import ProviderUnavailable

MAX_ATTEMPTS = 5
EVIDENCE_ERRORS = {'evidence_not_in_source', 'commitment_evidence_unavailable', 'commitment_promise_evidence_unavailable',
                   'commitment_deadline_evidence_unavailable', 'commitment_fulfillment_evidence_unavailable',
                   'commitment_fulfillment_date_invalid', 'commitment_fulfillment_ambiguous',
                   'commitment_cancellation_evidence_unavailable', 'commitment_cancellation_ambiguous'}
EVENT_TYPES = {"extract_message", "route_message", "extract_thread", "index_message"}
RETRYABLE = {
    "provider_rate_limited",
    "provider_overloaded",
    "provider_server_error",
    "provider_timeout",
    "provider_connection_failed",
    "provider_request_failed",
    "provider_response_error",
    "provider_invalid_response",
    "provider_output_truncated",
    "provider_in_flight_budget",
    "provider_circuit_open",
    "context_model_metadata_unavailable",
    "invalid_extraction_schema",
    "invalid_schema",
    "fact_thread_missing",
    "batch_classification_missing",
    "thread_classification_invalid",
    "thread_parent_invalid",
    "thread_sources_duplicate",
    "invalid_embedding",
    "routing_snapshot_changed",
    'pipeline_internal_error',
}


@transaction.atomic
def record_attempt(event, state, code="", next_at=None, *, focused_payload_sha256=None,
                   main_payload_sha256=None):
    if event.event_type not in {"extract_message", "route_message", "extract_thread"}:
        return
    traces = MessageProcessingTrace.objects.select_for_update()
    trace = (
        traces.filter(pk=event.payload["trace_id"]).first()
        if event.payload.get("trace_id")
        else traces.filter(operation_key=f"outbox:{event.id}").first()
    )
    if trace is None or (trace.status == "success" and state != "done"):
        return
    retry = trace.context_metadata.setdefault("retry", {})
    retry.update(
        state=state,
        attempts=event.attempt_count,
        max_attempts=MAX_ATTEMPTS,
        next_attempt=None if state == "cancelled" else event.attempt_count + 1,
        next_attempt_at=next_at.isoformat() if next_at else None,
    )
    if state in ("pending", "failed", "done"):
        history = retry.setdefault("history", [])
        history[:] = [row for row in history if row["attempt"] != event.attempt_count]
        history.append(
            {
                "attempt": event.attempt_count,
                "error_code": code,
                "finished_at": timezone.now().isoformat(),
            }
        )
        if focused_payload_sha256 is not None:
            history[-1]['focused_payload_sha256'] = focused_payload_sha256
        if main_payload_sha256 is not None:
            history[-1]['main_payload_sha256'] = main_payload_sha256
    if state == "cancelled":
        trace.status = "warning"
        trace.result_summary = "Запуск отменён администратором. Повторы этой попытки не назначаются."
    elif state in ("pending", "processing", "budget_wait", "rate_wait"):
        trace.status = "warning"
        trace.result_summary = (
            "Ожидает возобновления суточного бюджета AI."
            if state == "budget_wait"
            else "Ожидает освобождения минутного лимита AI."
            if state == "rate_wait"
            else f"Попыток выполнено: {event.attempt_count} из {MAX_ATTEMPTS}. "
            "Анализ будет повторён автоматически."
        )
        RawMessage.objects.filter(pk=trace.raw_message_id, processed=False).update(
            processing_state="received"
        )
    trace.save(update_fields=["context_metadata", "status", "result_summary"])


def _main_response_checkpoint(event):
    """Plan one retry-phase transition after durable native main progress."""
    trace = MessageProcessingTrace.objects.filter(pk=event.payload.get('trace_id')).first()
    if trace is None or trace.context_metadata.get('full_history_policy') != 'thread-context-v2':
        return None
    metadata = trace.context_metadata
    if metadata.get('request_state') != 'responded' or not metadata.get('main_response_cache'):
        return None
    from .pipeline import _main_response_cache, _restore_request
    try:
        value = _main_response_cache(trace, _restore_request(trace))
    except ProviderUnavailable:
        return None
    if value is None:
        return None
    marker = {'trace_id': trace.pk, 'response_sha256': metadata['main_response_cache']['response_sha256']}
    if event.payload.get('main_transport_checkpoint') == marker:
        return None
    retry = copy.deepcopy(metadata.get('retry', {}))
    retry['completed_main_phase'] = {'attempts': event.attempt_count, 'history': retry.get('history', []),
                                     'response_sha256': marker['response_sha256']}
    retry['history'], retry['phase'] = [], 'auxiliary'
    return trace, retry, marker


def _retryable_focused_project_rejection(trace, exc):
    return _focused_rejection_payload_sha(trace, exc) is not None


def _main_rejection_payload_sha(trace, exc):
    """Retry only the first observed routing rejection of an owned main request."""
    if (trace is None or not isinstance(exc, ProviderUnavailable)
            or str(exc) != 'provider_invalid_request' or trace.model_version != 'openrouter/free'):
        return None
    metadata, diagnostics = trace.context_metadata, exc.diagnostics
    if not isinstance(metadata, dict) or not isinstance(diagnostics, dict):
        return None
    if (metadata.get('full_history_policy') != 'thread-context-v2'
            or metadata.get('request_state') != 'failed'
            or type(diagnostics.get('provider_http_status')) is not int
            or type(diagnostics.get('provider_error_code')) is not int
            or any(diagnostics.get(key) != value for key, value in {
                'provider_http_status': 404, 'provider_error_code': 404,
                'provider_error_location': 'http', 'provider_error_type': 'unknown',
                'provider_error_category': 'unknown',
            }.items())
            or any(key in metadata for key in ('provider_result', 'main_response_cache',
                'focused_requests', 'focused_extraction', 'promise_identity', 'promise_reconciliation'))):
        return None
    from .context_tokens import payload_hash
    from .pipeline import _extraction_user_message, _restore_request
    try:
        retry = metadata.get('retry', {})
        history = retry.get('history', []) + retry.get('completed_main_phase', {}).get('history', [])
        if any(not isinstance(row, dict) or not isinstance(row.get('error_code'), str)
               or row['error_code'] == 'provider_invalid_request' for row in history):
            return None
        request = _restore_request(trace)
        if request.get('model') != 'openrouter/free':
            return None
        inputs = json.loads(_extraction_user_message(request)['content'])
        target = inputs.get('target_message_id')
        targets = metadata.get('batch_message_ids') or [trace.raw_message_id]
        declared = inputs.get('batch_message_ids') or [target]
        extra = [row['raw_message_id'] for row in inputs.get('target_messages', [])]
        if (type(target) is not int or target != trace.raw_message_id
                or not isinstance(targets, list) or any(type(pk) is not int for pk in targets)
                or len(set(targets)) != len(targets) or target not in targets
                or not isinstance(declared, list) or any(type(pk) is not int for pk in declared)
                or declared != targets
                or any(type(pk) is not int or pk not in targets or pk == target for pk in extra)
                or len(set(extra)) != len(extra)):
            return None
        return payload_hash(request)
    except (ProviderUnavailable, KeyError, TypeError, ValueError, AttributeError):
        return None


def _focused_rejection_payload_sha(trace, exc):
    """Replay one observed Free Router rejection after verifying auxiliary work."""
    if (trace is None or not isinstance(exc, ProviderUnavailable)
            or trace.model_version != 'openrouter/free'):
        return None
    code = str(exc)
    output_failure = code in {'invalid_extraction_schema', 'provider_output_truncated'}
    if code != 'provider_invalid_request' and not output_failure:
        return None
    metadata, diagnostics = trace.context_metadata, exc.diagnostics
    if not isinstance(metadata, dict) or not isinstance(diagnostics, dict):
        return None
    if (metadata.get('full_history_policy') != 'thread-context-v2'
            or metadata.get('request_state') != 'responded'):
        return None
    if not output_failure and (type(diagnostics.get('provider_http_status')) is not int
            or type(diagnostics.get('provider_error_code')) is not int
            or diagnostics.get('provider_error_category') != 'unknown'
            or (diagnostics.get('provider_http_status'), diagnostics.get('provider_error_code'),
                diagnostics.get('provider_error_location'), diagnostics.get('provider_error_type')) not in (
                    (200, 400, 'top_level', 'invalid_request'),
                    (404, 404, 'http', 'unknown'),
                )):
        return None
    from .context_tokens import payload_hash
    from .pipeline import _extraction_user_message, _main_response_cache, _restore_request
    try:
        main_request = _restore_request(trace)
        if _main_response_cache(trace, main_request) is None:
            return None
        requests = metadata.get('focused_requests', {})
        completed = metadata.get('focused_extraction', {})
        pending = [(key, row) for key, row in requests.items()
                   if not isinstance(completed.get(key), dict)
                   or completed[key].get('payload_sha256') != row.get('payload_sha256')]
        if len(pending) != 1:
            return None

        def owned_request_sha(key, row, allowed):
            request = row['request']
            digest = row['payload_sha256']
            if payload_hash(request) != digest or request.get('model') != 'openrouter/free':
                return None
            inputs = json.loads(_extraction_user_message(request)['content'])
            target, kinds = inputs.get('target_message_id'), inputs.get('analysis_fact_types')
            if (type(target) is int and target in (metadata.get('batch_message_ids') or [trace.raw_message_id])
                    and kinds in allowed and key == f'{target}:{kinds[0]}'
                    and inputs.get('analysis_operation') == 'focused_fact_extraction'):
                return digest
            return None

        empty_filtered_payment = (
            code == 'invalid_extraction_schema'
            and diagnostics.get('finish_reason') == 'content_filter'
            and type(diagnostics.get('output_tokens')) is int
            and diagnostics['output_tokens'] > 0
            and type(diagnostics.get('reasoning_tokens')) is int
            and diagnostics['reasoning_tokens'] == diagnostics['output_tokens']
            and type(diagnostics.get('response_characters')) is int
            and diagnostics['response_characters'] == 0
        )
        allowed = [['project']]
        if (not output_failure and diagnostics['provider_http_status'] == 404) or empty_filtered_payment:
            allowed.append(['payment'])
        digest = owned_request_sha(*pending[0], allowed)
        if digest is None:
            return None
        if output_failure and (diagnostics.get('focused_payload_sha256') != digest
                               or diagnostics.get('focused_cache_key') != pending[0][0]):
            return None
        retry = metadata.get('retry', {})
        history = retry.get('history', []) + retry.get('completed_main_phase', {}).get('history', [])
        seen = set()
        seen_main = False
        for previous in history:
            if not isinstance(previous, dict) or not isinstance(previous.get('error_code'), str):
                return None
            if previous['error_code'] not in {'provider_invalid_request', 'invalid_extraction_schema', 'provider_output_truncated'}:
                continue
            if 'main_payload_sha256' in previous:
                if (previous['error_code'] != 'provider_invalid_request'
                        or 'focused_payload_sha256' in previous or seen_main
                        or previous['main_payload_sha256'] != payload_hash(main_request)):
                    return None
                seen_main = True
                continue
            previous_digest = previous.get('focused_payload_sha256')
            if not isinstance(previous_digest, str) or previous_digest == digest or previous_digest in seen:
                return None
            seen.add(previous_digest)
            proven = False
            previous_allowed = [['project'], ['payment']] if previous['error_code'] in {'provider_invalid_request', 'invalid_extraction_schema'} else [['project']]
            for key, row in requests.items():
                saved = completed.get(key)
                if (isinstance(saved, dict) and saved.get('payload_sha256') == previous_digest
                        and isinstance(saved.get('result'), dict)
                        and isinstance(saved['result'].get('facts'), list)
                        and isinstance(saved['result'].get('topic_updates'), list)
                        and owned_request_sha(key, row, previous_allowed) == previous_digest):
                    proven = True
                    break
            if not proven:
                return None
        return digest
    except (ProviderUnavailable, KeyError, TypeError, ValueError, AttributeError):
        return None


def _sol_focused_rejection(trace, exc):
    """Recognize only an owned GPT Sol auxiliary failure after verified main work."""
    from .gpt_sol import MODEL, POLICY, LEGACY_POLICY, LITERAL_POLICY, REPAIRABLE_OUTPUT
    if (trace is None or not isinstance(exc, ProviderUnavailable)
            or trace.model_version != MODEL or str(exc) not in REPAIRABLE_OUTPUT):
        return None
    metadata, diagnostics = trace.context_metadata, exc.diagnostics
    if (not isinstance(metadata, dict) or not isinstance(diagnostics, dict)
            or metadata.get('full_history_policy') != 'thread-context-v2'
            or metadata.get('request_state') != 'responded'):
        return None
    key, digest = diagnostics.get('focused_cache_key'), diagnostics.get('focused_payload_sha256')
    if (not isinstance(key, str) or len(key) > 256 or not isinstance(digest, str)
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        return None
    from .context_tokens import payload_hash
    from .pipeline import _extraction_user_message, _main_response_cache, _restore_request
    try:
        if _main_response_cache(trace, _restore_request(trace)) is None:
            return None
        row = metadata.get('focused_requests', {}).get(key)
        if not isinstance(row, dict) or row.get('payload_sha256') != digest:
            return None
        request = row['request']
        if request.get('model') != MODEL or payload_hash(request) != digest:
            return None
        inputs = json.loads(_extraction_user_message(request)['content'])
        target, kinds = inputs.get('target_message_id'), inputs.get('analysis_fact_types')
        targets = metadata.get('batch_message_ids') or [trace.raw_message_id]
        policy = inputs.get('analysis_transport', POLICY)
        if (not isinstance(targets, list) or any(type(pk) is not int for pk in targets)
                or type(target) is not int or target not in targets
                or kinds not in (['project'], ['payment'], ['commitment'])
                or policy not in (POLICY, LEGACY_POLICY, LITERAL_POLICY)
                or inputs.get('analysis_operation') != 'focused_fact_extraction'
                or key not in (f'{target}:{kinds[0]}', f'{target}:{kinds[0]}:{policy}',
                               f'{target}:{kinds[0]}:{policy}:{digest}')):
            return None
        sources = {target}
        for item in inputs.get('context', []) + inputs.get('target_messages', []):
            if not isinstance(item, dict) or type(item.get('raw_message_id')) is not int:
                return None
            sources.add(item['raw_message_id'])
        window = metadata.get('context_window', {})
        source_ids = window.get('source_ids') if isinstance(window, dict) else None
        if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 51
                or any(type(pk) is not int for pk in source_ids)
                or len(set(source_ids)) != len(source_ids) or not sources <= set(source_ids)):
            return None
        completed = metadata.get('focused_extraction', {}).get(key)
        if isinstance(completed, dict) and completed.get('payload_sha256') == digest:
            return None
        rejection = {'version': 'gpt-sol-focused-rejection-v1', 'payload_sha256': digest,
                'focused_cache_key': key, 'target_message_id': target, 'fact_type': kinds[0],
                'error_code': str(exc), 'schema_errors': copy.deepcopy(diagnostics.get('schema_errors', [])[:24])}
        base_digest = row.get('base_payload_sha256')
        if isinstance(base_digest, str) and re.fullmatch(r'[0-9a-f]{64}', base_digest):
            rejection['base_payload_sha256'] = base_digest
        return rejection
    except (KeyError, TypeError, ValueError, AttributeError, ProviderUnavailable):
        return None


def _retry_sol_focused(current, trace, rejection, exc):
    """Bound auxiliary retries on the same immutable main response and trace."""
    checkpoint = _main_response_checkpoint(current)
    if checkpoint is not None:
        completed, retry, marker = checkpoint
        completed.context_metadata['retry'] = retry
        completed.save(update_fields=['context_metadata'])
        current.payload = {**current.payload, 'main_transport_checkpoint': marker}
        current.attempt_count = 1
    trace.refresh_from_db()
    key = rejection['focused_cache_key']
    records = trace.context_metadata.setdefault('focused_rejections', {})
    previous = records.get(key, {})
    history = previous.get('attempts', []) if previous.get('payload_sha256') == rejection['payload_sha256'] else []
    # Preserve each observed rejection; feedback for the next request is a
    # separate map and never replaces this exact-envelope audit history.
    recorded = previous if history else rejection
    records[key] = {**recorded, 'attempts': [*history, {
        'attempt': current.attempt_count, 'at': timezone.now().isoformat(),
        'error_code': str(exc), 'schema_errors': copy.deepcopy(rejection['schema_errors'])}]}
    trace.save(update_fields=['context_metadata'])
    pending = current.attempt_count < 3
    delay = max((15 if current.attempt_count == 1 else 45) + random.randint(0, 5), exc.retry_after or 0)
    current.state, current.lease_until = 'pending' if pending else 'failed', None
    current.error_code = str(exc)
    if pending:
        current.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    current.save()
    record_attempt(current, current.state, str(exc), current.next_attempt_at if pending else None,
                   focused_payload_sha256=rejection['payload_sha256'])
    return True


def _retry_focused_context(current, exc):
    """Reselect only unvalidated fresh context; never rewrite a frozen page."""
    if current.event_type != 'extract_message' or not current.payload.get('trace_id'):
        return False
    previous = MessageProcessingTrace.objects.get(pk=current.payload['trace_id'])
    metadata = previous.context_metadata
    state = current.payload.get('full_history_state') or {}
    full = metadata.get('full_history', {})
    if (metadata.get('full_history_policy') != 'thread-context-v2'
            or metadata.get('token_counter') != 'router-utf8-bound-v1'
            or state.get('page') or state.get('phase', 'read') != 'read'
            or full.get('phase') != 'read'):
        return False
    targets = set(current.payload.get('batch_ids') or [current.payload['raw_id']])
    focused_ids = set(exc.diagnostics.get('focused_context_ids', []))
    before, after = state.get('ranges', {}), full.get('ranges', {})
    fresh_ids = [item['raw_message_id'] for item in previous.earlier_messages_context
                 if item['raw_message_id'] in focused_ids - targets
                 and item.get('content') and not item.get('partial') and not item.get('evidence_excerpt')
                 and after.get(str(item['raw_message_id']), 0) - before.get(str(item['raw_message_id']), 0) >= len(item['content'])]
    if not fresh_ids:
        return False
    limits = dict(metadata.get('analysis_limits', {}))
    stage = len(state.get('stage_trace_ids', []))
    generation = limits.get('focused_context_retry_generation', 0) if limits.get('focused_context_retry_stage') == stage else 0
    available = metadata.get('effective_input_budget')
    cap = metadata.get('analysis_chunk_input_limit') or available
    ceiling = limits.get('focused_context_input_ceiling')
    if type(ceiling) is int and ceiling > 0 and type(cap) is int:
        cap = min(cap, ceiling)
    if (type(generation) is not int or not 0 <= generation < 4
            or type(available) is not int or type(cap) is not int or cap <= 512):
        return False
    cap = max(512, min(cap, available) // 2)
    limits.update(focused_context_input_ceiling=cap,
                  focused_context_ceiling_policy=metadata.get('native_enum_policy'),
                  focused_context_retry_stage=stage,
                  focused_context_retry_generation=generation + 1)
    from .processing_attempts import reserve_attempt
    raw = RawMessage.objects.select_for_update().get(pk=current.payload['raw_id'])
    trace = reserve_attempt(raw, f'{current.deduplication_key}:focused-context:{stage}:{generation + 1}:{previous.id}')
    trace.context_metadata.update({key: metadata[key] for key in (
        'analysis_policy', 'full_history_policy', 'snapshot_max_id', 'full_history',
        'replace_unsent', 'required_thread_keys', 'output_retry_generation', 'context_window') if key in metadata})
    from .free_fallback import policy_metadata
    trace.context_metadata.update(policy_metadata(metadata))
    trace.context_metadata.update(analysis_limits=limits, analysis_chunk_input_limit=cap,
                                  repair_reason=str(exc), parent_trace_id=previous.id,
                                  focused_context_retry_fresh_ids=fresh_ids)
    trace.save(update_fields=['context_metadata'])
    record_attempt(current, 'failed', str(exc))
    members = [entry for entry in current.payload.get('trace_ids', []) if entry['raw_id'] != raw.id]
    members.append({'raw_id': raw.id, 'trace_id': trace.id})
    # full_history_state (including result, ranges and any cursor) stays exact.
    current.payload = {**current.payload, 'trace_id': trace.id, 'trace_ids': members}
    current.state, current.lease_until = 'pending', None
    current.error_code, current.attempt_count = str(exc), 0
    current.next_attempt_at = timezone.now() + timedelta(seconds=1)
    current.save()
    from .models import HistoryAnalysisItem
    HistoryAnalysisItem.objects.filter(outbox_event=current, raw_message_id=raw.id).update(trace=trace)
    record_attempt(current, 'pending', str(exc), current.next_attempt_at)
    return True


@transaction.atomic
def handle_failure(event, exc):
    if event.event_type not in EVENT_TYPES or not isinstance(exc, ProviderUnavailable):
        return False
    current = OutboxEvent.objects.select_for_update().get(pk=event.pk)
    if event.payload.get("claim_generation") and current.payload.get("claim_generation") != event.payload["claim_generation"]:
        return True
    if current.state == "cancelled" or current.payload.get("history_cancelled"):
        current.state, current.lease_until = "cancelled", None
        current.save(update_fields=["state", "lease_until"])
        record_attempt(current, "cancelled", "history_run_cancelled")
        return True
    code = str(exc)
    if not current.payload.get("analysis_policy"):
        trace = MessageProcessingTrace.objects.filter(pk=current.payload["trace_id"]).first() if current.payload.get("trace_id") else MessageProcessingTrace.objects.filter(operation_key=f"outbox:{current.id}").first()
        if trace and trace.context_metadata.get("analysis_policy") == "history-packets-v1":
            current.payload = {**current.payload, "analysis_policy":"history-packets-v1", "trace_id":trace.id,
                               "trace_ids":[{"raw_id":trace.raw_message_id,"trace_id":trace.id}],
                               "root_event_id":current.id}
            current.save(update_fields=["payload"])
    if current.payload.get("analysis_policy") == "history-packets-v1":
        if code == 'focused_context_budget_exceeded' and _retry_focused_context(current, exc):
            return True
        focused_payload_sha256 = None
        rejected_trace = None
        from .gpt_sol import REPAIRABLE_OUTPUT as SOL_REPAIRABLE_OUTPUT
        if code in SOL_REPAIRABLE_OUTPUT:
            rejected_trace = MessageProcessingTrace.objects.filter(pk=current.payload.get('trace_id')).first()
            sol_rejection = _sol_focused_rejection(rejected_trace, exc)
            if sol_rejection is not None:
                return _retry_sol_focused(current, rejected_trace, sol_rejection, exc)
        if code in {'provider_invalid_request', 'invalid_extraction_schema', 'provider_output_truncated'}:
            if rejected_trace is None:
                rejected_trace = MessageProcessingTrace.objects.filter(pk=current.payload.get('trace_id')).first()
            focused_payload_sha256 = _focused_rejection_payload_sha(rejected_trace, exc)
        if (focused_payload_sha256 is not None and code in {'invalid_extraction_schema', 'provider_output_truncated'}
                and current.attempt_count >= 3 and _main_response_checkpoint(current) is None):
            focused_payload_sha256 = None
        structural = {"source_revision_changed", "skipped_crm_delivered", "blocked_delivery", "projection_changed", "projection_snapshot_missing", "deduplication_ambiguous", "extraction_json_parse", "extraction_top_level_type", "extraction_facts_missing", "extraction_facts_type", "invalid_extraction_schema", "invalid_schema", "batch_classification_missing", "thread_classification_invalid", "thread_completion_unproven", "thread_sources_unavailable", "thread_source_conflict", "thread_sources_duplicate", "thread_keys_duplicate", "thread_parent_invalid", "thread_revision_conflict", "target_classification_invalid", "target_classification_conflict", "fact_thread_missing", "fact_thread_evidence_missing", "fact_thread_evidence_conflict", "provider_output_truncated", "context_batch_too_large", "context_fixed_input_too_large"}
        structural |= EVIDENCE_ERRORS
        from .history_analysis import split
        if code in structural and focused_payload_sha256 is None:
            current.error_code = code
            current.save(update_fields=["error_code"])
            if split(current):
                record_attempt(current, "failed", code)
                return True
            from .replay_replacement import LABELS
            if code in LABELS:
                from .models import HistoryAnalysisItem
                current.state,current.lease_until="done",None
                current.save(update_fields=["state","lease_until"])
                HistoryAnalysisItem.objects.filter(outbox_event=current).update(state="succeeded" if code=="skipped_crm_delivered" else "blocked",disposition=code if code=="skipped_crm_delivered" else "blocked",reason_code=code,reason_description=LABELS[code])
                return True
            if code in ("context_fixed_input_too_large", "provider_output_truncated"):
                from .history_packets import prepare_segments
                previous = MessageProcessingTrace.objects.get(pk=current.payload['trace_id'])
                if previous.context_metadata.get('full_history_policy') != 'thread-context-v2' and prepare_segments(current, output_overflow=code == "provider_output_truncated"):
                    return True
            previous = MessageProcessingTrace.objects.get(pk=current.payload['trace_id'])
            full_output = code == 'provider_output_truncated' and previous.context_metadata.get('full_history_policy') == 'thread-context-v2'
            output_generation = previous.context_metadata.get('output_retry_generation', 0)
            if (full_output and output_generation < 4) or (not full_output and not current.payload.get("schema_repaired") and code != "context_fixed_input_too_large"):
                from .processing_attempts import reserve_attempt
                raw = RawMessage.objects.select_for_update().get(pk=current.payload["raw_id"])
                previous = raw.traces.get(pk=current.payload["trace_id"])
                repair_key = f"{current.deduplication_key}:schema-repair"
                if previous.context_metadata.get('full_history_policy') == 'thread-context-v2':
                    repair_key += f':{previous.id}'
                trace = reserve_attempt(raw, repair_key)
                limits = dict(previous.context_metadata.get("analysis_limits", {}))
                if previous.context_metadata.get('full_history_policy') not in {'thread-context-v2', 'thread-routed-context-v1'} and code in {'context_batch_too_large', 'provider_output_truncated'}:
                    limits["analysis_input_token_limit"] = max(4096, limits.get("analysis_input_token_limit", 16384) // 2)
                elif full_output:
                    from .models import AISettings
                    cfg = AISettings.get_active()
                    limits['analysis_output_token_limit'] = min(cfg.max_completion_tokens, limits.get('analysis_output_token_limit', cfg.analysis_output_token_limit) * 2)
                    trace.context_metadata['output_retry_generation'] = output_generation + 1
                    if limits['analysis_output_token_limit'] == previous.context_metadata['completion_reserve_tokens']:
                        trace.context_metadata['analysis_chunk_input_limit'] = max(512,
                            (previous.context_metadata.get('analysis_chunk_input_limit') or previous.context_metadata['input_tokens_preflight']) // 2)
                trace.context_metadata.update(analysis_policy="history-packets-v1", analysis_limits=limits, repair_reason=code)
                # None deliberately selects a bounded packet. reserve_attempt
                # defaults to full history, so absence must not widen a retry.
                trace.context_metadata['full_history_policy'] = previous.context_metadata.get('full_history_policy')
                if code in {'target_classification_invalid', 'fact_thread_missing'}:
                    result = previous.context_metadata.get('provider_result') or {}
                    classifications = result.get('target_classifications', {})
                    references = list(classifications.values()) if isinstance(classifications, dict) else classifications if isinstance(classifications, list) else []
                    facts = result.get('facts', [])
                    references += [result.get('target_classification', {})] + (facts if isinstance(facts, list) else [])
                    allowed = {f't{i}' for i in range(16)}
                    trace.context_metadata['required_thread_keys'] = sorted({row.get('thread_key') for row in references
                        if isinstance(row, dict) and isinstance(row.get('thread_key'), str) and row['thread_key'] in allowed})
                for field in ('full_history_policy', 'full_history', 'analysis_chunk_input_limit', 'output_retry_generation', 'context_window'):
                    if field in previous.context_metadata:
                        if field in ('analysis_chunk_input_limit', 'output_retry_generation'):
                            trace.context_metadata.setdefault(field, previous.context_metadata[field])
                        else:
                            trace.context_metadata[field] = previous.context_metadata[field]
                for field in ("replace_unsent", "snapshot_max_id"):
                    if field in previous.context_metadata:trace.context_metadata[field]=previous.context_metadata[field]
                from .free_fallback import policy_metadata
                trace.context_metadata.update(policy_metadata(previous.context_metadata))
                trace.save(update_fields=["context_metadata"])
                current.payload = {**current.payload, "trace_id": trace.id, "trace_ids": [{"raw_id":raw.id,"trace_id":trace.id}], "schema_repaired":current.payload.get('schema_repaired', False) if full_output else True}
                if current.payload.get("segment_trace_ids"):
                    trace_list = list(current.payload["segment_trace_ids"])
                    trace_list[current.payload["segment_cursor"]] = trace.id
                    current.payload["segment_trace_ids"] = trace_list
                current.state, current.lease_until, current.next_attempt_at = "pending", None, timezone.now() + timedelta(seconds=1)
                current.attempt_count = 0
                current.save()
                from .models import HistoryAnalysisItem
                if not current.payload.get("segment_trace_ids") or current.payload["segment_cursor"] == len(current.payload["segment_trace_ids"])-1:
                    HistoryAnalysisItem.objects.filter(outbox_event=current).update(trace=trace)
                record_attempt(current, "pending", code, current.next_attempt_at)
                return True
        transport = {"provider_timeout", "provider_connection_failed", "provider_server_error", "provider_rate_limited", "provider_overloaded", "provider_invalid_response", "provider_in_flight_budget", "provider_circuit_open", "context_model_metadata_unavailable", "routing_snapshot_changed"}
        main_payload_sha256 = None
        if code == 'provider_invalid_request' and focused_payload_sha256 is None:
            main_payload_sha256 = _main_rejection_payload_sha(rejected_trace, exc)
        if focused_payload_sha256 is not None or main_payload_sha256 is not None:
            transport.add(code)
        if code in transport - {'provider_circuit_open', 'context_model_metadata_unavailable', 'routing_snapshot_changed'}:
            checkpoint = _main_response_checkpoint(current)
            if checkpoint is not None:
                completed, retry, marker = checkpoint
                completed.context_metadata['retry'] = retry
                completed.save(update_fields=['context_metadata'])
                current.payload = {**current.payload, 'main_transport_checkpoint': marker}
                current.attempt_count = 1
        pending = code in transport and current.attempt_count < 3
        delay = max((15 if current.attempt_count == 1 else 45) + random.randint(0, 5), exc.retry_after or 0)
        current.state, current.lease_until = "pending" if pending else "failed", None
        current.error_code = code
        if pending:
            current.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        current.save()
        record_attempt(current, current.state, code, current.next_attempt_at if pending else None,
                       focused_payload_sha256=focused_payload_sha256,
                       main_payload_sha256=main_payload_sha256)
        return True
    pending = code in RETRYABLE and event.attempt_count < MAX_ATTEMPTS
    delay = 60 * 2 ** max(0, min(event.attempt_count - 1, 3)) + random.randint(0, 15)
    delay = max(delay, exc.retry_after or 0)
    next_at = timezone.now() + timedelta(seconds=delay) if pending else None
    payload = dict(event.payload)
    batch = payload.get("batch_ids", [])
    if code in {"invalid_extraction_schema", "invalid_schema", "batch_classification_missing", "thread_classification_invalid", "provider_output_truncated"} and len(batch) > 1:
        payload["batch_ids"] = batch[:max(1, len(batch) // 2)]
        from .processing_attempts import reserve_attempt
        # Preserve the old immutable envelope; a smaller request is a distinct
        # trace, not an in-place rewrite of the previous model input.
        with transaction.atomic():
            raw = RawMessage.objects.select_for_update().get(pk=payload["raw_id"])
            replacement = reserve_attempt(raw, f"packet-repair:{event.id}:{len(payload['batch_ids'])}")
            payload["trace_id"] = replacement.id
    OutboxEvent.objects.filter(pk=event.id).update(
        payload=payload,
        state="pending" if pending else "failed",
        error_code=code,
        lease_until=None,
        **({"next_attempt_at": next_at} if pending else {}),
    )
    record_attempt(event, "pending" if pending else "failed", code, next_at)
    return True
