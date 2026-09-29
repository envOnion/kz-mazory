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
    "provider_in_flight_budget",
    "context_model_metadata_unavailable",
    "invalid_extraction_schema",
    "invalid_schema",
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
        next_attempt=event.attempt_count + 1,
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
    if state in ("pending", "processing", "budget_wait"):
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


def handle_failure(event, exc):
    if event.event_type not in EVENT_TYPES or not isinstance(exc, ProviderUnavailable):
        return False
    code = str(exc)
    pending = code in RETRYABLE and event.attempt_count < MAX_ATTEMPTS
    delay = 60 * 2 ** max(0, min(event.attempt_count - 1, 3)) + random.randint(0, 15)
    delay = max(delay, exc.retry_after or 0)
    next_at = timezone.now() + timedelta(seconds=delay) if pending else None
    OutboxEvent.objects.filter(pk=event.id).update(
        state="pending" if pending else "failed",
        error_code=code,
        lease_until=None,
        **({"next_attempt_at": next_at} if pending else {}),
    )
    record_attempt(event, "pending" if pending else "failed", code, next_at)
    return True
