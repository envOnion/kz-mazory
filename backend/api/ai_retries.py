"""Durable retries for background message AI work; no sleeping worker threads."""

import copy
import json
import random
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
def record_attempt(event, state, code="", next_at=None):
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
    """Replay one observed Free Router rejection after verifying auxiliary work."""
    if (trace is None or not isinstance(exc, ProviderUnavailable)
            or str(exc) != 'provider_invalid_request' or trace.model_version != 'openrouter/free'):
        return False
    metadata, diagnostics = trace.context_metadata, exc.diagnostics
    if not isinstance(metadata, dict) or not isinstance(diagnostics, dict):
        return False
    if (metadata.get('full_history_policy') != 'thread-context-v2'
            or metadata.get('request_state') != 'responded'
            or type(diagnostics.get('provider_http_status')) is not int
            or type(diagnostics.get('provider_error_code')) is not int
            or diagnostics.get('provider_error_category') != 'unknown'
            or (diagnostics.get('provider_http_status'), diagnostics.get('provider_error_code'),
                diagnostics.get('provider_error_location'), diagnostics.get('provider_error_type')) not in (
                    (200, 400, 'top_level', 'invalid_request'),
                    (404, 404, 'http', 'unknown'),
                )):
        return False
    from .context_tokens import payload_hash
    from .pipeline import _extraction_user_message, _main_response_cache, _restore_request
    try:
        retry = metadata.get('retry', {})
        history = retry.get('history', []) + retry.get('completed_main_phase', {}).get('history', [])
        if any(row.get('error_code') == 'provider_invalid_request' for row in history):
            return False
        if _main_response_cache(trace, _restore_request(trace)) is None:
            return False
        completed = metadata.get('focused_extraction', {})
        pending = [(key, row) for key, row in metadata.get('focused_requests', {}).items()
                   if not isinstance(completed.get(key), dict)
                   or completed[key].get('payload_sha256') != row.get('payload_sha256')]
        if len(pending) != 1:
            return False
        key, row = pending[0]
        request = row['request']
        if payload_hash(request) != row['payload_sha256'] or request.get('model') != 'openrouter/free':
            return False
        inputs = json.loads(_extraction_user_message(request)['content'])
        target = inputs.get('target_message_id')
        kinds = inputs.get('analysis_fact_types')
        allowed = [['project'], ['payment']] if diagnostics['provider_http_status'] == 404 else [['project']]
        return (type(target) is int and target in (metadata.get('batch_message_ids') or [trace.raw_message_id])
                and kinds in allowed and key == f'{target}:{kinds[0]}'
                and inputs.get('analysis_operation') == 'focused_fact_extraction')
    except (ProviderUnavailable, KeyError, TypeError, ValueError, AttributeError):
        return False


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
        'replace_unsent', 'required_thread_keys', 'output_retry_generation') if key in metadata})
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
        structural = {"source_revision_changed", "skipped_crm_delivered", "blocked_delivery", "projection_changed", "projection_snapshot_missing", "deduplication_ambiguous", "extraction_json_parse", "extraction_top_level_type", "extraction_facts_missing", "extraction_facts_type", "invalid_extraction_schema", "invalid_schema", "batch_classification_missing", "thread_classification_invalid", "thread_completion_unproven", "thread_sources_unavailable", "thread_source_conflict", "thread_sources_duplicate", "thread_keys_duplicate", "thread_parent_invalid", "thread_revision_conflict", "target_classification_invalid", "target_classification_conflict", "fact_thread_missing", "fact_thread_evidence_missing", "fact_thread_evidence_conflict", "provider_output_truncated", "context_batch_too_large", "context_fixed_input_too_large"}
        structural |= EVIDENCE_ERRORS
        from .history_analysis import split
        if code in structural:
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
                for field in ('full_history_policy', 'full_history', 'analysis_chunk_input_limit', 'output_retry_generation'):
                    if field in previous.context_metadata:
                        if field in ('analysis_chunk_input_limit', 'output_retry_generation'):
                            trace.context_metadata.setdefault(field, previous.context_metadata[field])
                        else:
                            trace.context_metadata[field] = previous.context_metadata[field]
                for field in ("replace_unsent", "snapshot_max_id"):
                    if field in previous.context_metadata:trace.context_metadata[field]=previous.context_metadata[field]
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
        transport = {"provider_timeout", "provider_connection_failed", "provider_server_error", "provider_rate_limited", "provider_overloaded", "provider_in_flight_budget", "provider_circuit_open", "context_model_metadata_unavailable", "routing_snapshot_changed"}
        if code == 'provider_invalid_request' and _retryable_focused_project_rejection(
                MessageProcessingTrace.objects.filter(pk=current.payload.get('trace_id')).first(), exc):
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
        record_attempt(current, current.state, code, current.next_attempt_at if pending else None)
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
