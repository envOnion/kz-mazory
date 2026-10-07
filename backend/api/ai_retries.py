"""Durable retries for background message AI work; no sleeping worker threads."""

import random
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import MessageProcessingTrace, OutboxEvent, RawMessage
from .providers import ProviderUnavailable

MAX_ATTEMPTS = 5
EVENT_TYPES = {"extract_message", "index_message"}
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
    "invalid_embedding",
}


@transaction.atomic
def record_attempt(event, state, code="", next_at=None):
    if event.event_type != "extract_message":
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
    elif state in ("pending", "processing", "budget_wait"):
        trace.status = "warning"
        trace.result_summary = (
            "Ожидает возобновления суточного бюджета AI."
            if state == "budget_wait"
            else f"Попыток выполнено: {event.attempt_count} из {MAX_ATTEMPTS}. "
            "Анализ будет повторён автоматически."
        )
        RawMessage.objects.filter(pk=trace.raw_message_id, processed=False).update(
            processing_state="received"
        )
    trace.save(update_fields=["context_metadata", "status", "result_summary"])


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
        structural = {"extraction_json_parse", "extraction_top_level_type", "extraction_facts_missing", "extraction_facts_type", "invalid_extraction_schema", "invalid_schema", "batch_classification_missing", "thread_classification_invalid", "thread_completion_unproven", "thread_sources_unavailable", "thread_source_conflict", "thread_sources_duplicate", "thread_keys_duplicate", "thread_parent_invalid", "thread_revision_conflict", "target_classification_invalid", "target_classification_conflict", "fact_thread_missing", "fact_thread_evidence_missing", "fact_thread_evidence_conflict", "provider_output_truncated", "context_batch_too_large", "context_fixed_input_too_large"}
        from .history_analysis import split
        if code in structural:
            current.error_code = code
            current.save(update_fields=["error_code"])
            if split(current):
                record_attempt(current, "failed", code)
                return True
            if code in ("context_fixed_input_too_large", "provider_output_truncated"):
                from .history_packets import prepare_segments
                if prepare_segments(current, output_overflow=code == "provider_output_truncated"):
                    return True
            if not current.payload.get("schema_repaired") and code != "context_fixed_input_too_large":
                from .processing_attempts import reserve_attempt
                raw = RawMessage.objects.select_for_update().get(pk=current.payload["raw_id"])
                previous = raw.traces.get(pk=current.payload["trace_id"])
                trace = reserve_attempt(raw, f"{current.deduplication_key}:schema-repair")
                limits = dict(previous.context_metadata.get("analysis_limits", {}))
                limits["analysis_input_token_limit"] = max(4096, limits.get("analysis_input_token_limit", 16384) // 2)
                trace.context_metadata.update(analysis_policy="history-packets-v1", analysis_limits=limits, repair_reason=code)
                trace.save(update_fields=["context_metadata"])
                current.payload = {**current.payload, "trace_id": trace.id, "trace_ids": [{"raw_id":raw.id,"trace_id":trace.id}], "schema_repaired":True}
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
        transport = {"provider_timeout", "provider_connection_failed", "provider_server_error", "provider_rate_limited", "provider_overloaded", "provider_in_flight_budget", "provider_circuit_open", "context_model_metadata_unavailable"}
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
