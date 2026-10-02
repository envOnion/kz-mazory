"""Coalesce new-chat activity into asynchronous rechecks of existing promises."""

from django.db import transaction
from .message_context import source_scope
from .models import FactCandidate, OutboxEvent, RawMessage
from .processing_attempts import reserve_attempt


def schedule_commitment_refresh(raw):
    scope = source_scope(raw)
    origins = (
        FactCandidate.objects.filter(
            trace__raw_message__in=scope.exclude(pk=raw.id),
            fact_type="commitment",
            status__in=["pending", "approved"],
        )
        .exclude(accepted_commitment__status__in=["fulfilled", "cancelled"])
        .values_list("trace__raw_message_id", flat=True)
        .distinct()
    )
    count = 0
    for origin_id in origins.iterator():
        with transaction.atomic():
            source = RawMessage.objects.select_for_update().get(pk=origin_id)
            # A queued recheck will snapshot the latest chat at execution time.
            if OutboxEvent.objects.filter(
                event_type="extract_message",
                payload__raw_id=origin_id,
                payload__commitment_refresh=True,
                state__in=["pending", "enqueued"],
            ).exists():
                continue
            key = f"commitment-refresh:{origin_id}:{raw.id}"
            trace = reserve_attempt(source, key)
            _, created = OutboxEvent.objects.get_or_create(
                deduplication_key=key,
                defaults={
                    "event_type": "extract_message",
                    "payload": {
                        "raw_id": origin_id,
                        "trace_id": trace.id,
                        "commitment_refresh": True,
                    },
                },
            )
            count += created
    return count
