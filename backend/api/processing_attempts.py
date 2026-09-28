"""Immutable attempt identity and explicit, permission-checked reanalysis scheduling."""

from django.db import transaction
from django.db.models import Max
from rest_framework.exceptions import PermissionDenied

from . import access
from .message_context import POLICY, source_scope
from .models import AuditEvent, MessageProcessingTrace, OutboxEvent, RawMessage
from .providers import ProviderUnavailable


def require_reanalysis(user, raw):
    if not user.is_active or not access.messages_for(user).filter(pk=raw.pk).exists():
        raise PermissionDenied("Нет доступа к исходному сообщению.")
    team_id = raw.config.team_id if raw.config_id else raw.team_id
    access.require_team_role(user, team_id, ["team_lead"])
    source_scope(raw)


def reserve_attempt(raw, operation_key):
    """Caller holds the source row lock; the unique constraint is the final guard."""
    existing = MessageProcessingTrace.objects.filter(
        operation_key=operation_key
    ).first()
    if existing:
        return existing
    attempt = (raw.traces.aggregate(last=Max("attempt_no"))["last"] or 0) + 1
    return MessageProcessingTrace.objects.create(
        raw_message=raw,
        operation_key=operation_key,
        attempt_no=attempt,
        whatsapp_message_id=raw.message_id,
        whatsapp_chat_id=raw.chat_id,
        whatsapp_sender_phone=raw.sender_phone,
        whatsapp_sender_name=raw.sender_name,
        whatsapp_timestamp=raw.timestamp if raw.sent_at_known else None,
        whatsapp_content=raw.content,
        prompt_version="facts-v2-full-history",
        status="warning",
        result_summary="Ожидает обработки с полной историей.",
        context_metadata={
            "schema_version": 1,
            "policy_version": POLICY,
            "source": "chat_history",
            "request_state": "not_sent",
            "snapshot_max_id": RawMessage.objects.aggregate(last=Max("id"))["last"]
            or raw.id,
        },
    )


def schedule_reanalysis(raw_id, user, request_key):
    with transaction.atomic():
        raw = (
            RawMessage.objects.select_for_update(of=("self",))
            .select_related("config__team", "team", "project")
            .get(pk=raw_id)
        )
        require_reanalysis(user, raw)
        operation_key = f"reanalysis:{raw.id}:{request_key}"
        previous = MessageProcessingTrace.objects.filter(
            operation_key=operation_key
        ).first()
        if previous:
            return previous
        active = OutboxEvent.objects.filter(
            event_type="extract_message",
            payload__raw_id=raw.id,
            state__in=["pending", "enqueued", "processing"],
        ).first()
        if active:
            trace_id = active.payload.get("trace_id")
            if trace_id:
                return raw.traces.get(pk=trace_id)
            raise ProviderUnavailable("context_processing_in_progress")
        trace = reserve_attempt(raw, operation_key)
        OutboxEvent.objects.create(
            event_type="extract_message",
            deduplication_key=operation_key,
            payload={
                "raw_id": raw.id,
                "trace_id": trace.id,
                "requested_by_id": user.id,
            },
        )
        AuditEvent.objects.create(
            actor=user,
            target_type="MessageProcessingTrace",
            target_id=trace.id,
            action="reanalysis_requested",
            before_after={"raw_message_id": raw.id, "attempt_no": trace.attempt_no},
        )
        return trace
