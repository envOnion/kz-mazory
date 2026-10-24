"""Immutable attempt identity and explicit, permission-checked reanalysis scheduling."""

from django.db import transaction
from django.db.models import Max, Q
from rest_framework.exceptions import PermissionDenied

from . import access
from .message_context import POLICY, source_scope
from .models import AISettings, AuditEvent, MessageProcessingTrace, OutboxEvent, RawMessage
from .providers import ProviderUnavailable

ANALYSIS_CONFIG_FIELDS = ('chat_model_name', 'chat_api_format', 'chat_provider_url', 'context_window_tokens',
                          'context_safety_tokens', 'max_completion_tokens', 'tokenizer_id', 'tokenizer_revision')
_UNSET = object()

def require_reanalysis(user, raw):
    if not user.is_active or not access.messages_for(user).filter(pk=raw.pk).exists():
        raise PermissionDenied("Нет доступа к исходному сообщению.")
    team_id = raw.config.team_id if raw.config_id else raw.team_id
    access.require_team_role(user, team_id, ["team_lead"])
    if raw.processing_state == "offtopic":
        raise PermissionDenied("Сообщение помечено как оффтоп и исключено из автоматического анализа.")
    if raw.processing_state == 'clarification_reply':
        raise PermissionDenied("Ответ на уточнение сохранён как человеческий факт и не передаётся на повторный анализ модели.")
    source_scope(raw)


def reserve_attempt(raw, operation_key, *, cfg=None, snapshot_max_id=None,
                    existing=_UNSET, latest_attempt_no=None):
    """Caller holds the source row lock; the unique constraint is the final guard."""
    if raw.processing_state == "offtopic":
        raise ProviderUnavailable("offtopic_not_model_input")
    if raw.processing_state == 'clarification_reply':
        raise ProviderUnavailable('clarification_reply_not_model_input')
    if existing is _UNSET:
        existing = MessageProcessingTrace.objects.filter(
            operation_key=operation_key
        ).first()
    if existing:
        return existing
    attempt = (latest_attempt_no if latest_attempt_no is not None else
               raw.traces.aggregate(last=Max("attempt_no"))["last"] or 0) + 1
    cfg = cfg or AISettings.get_active()
    from .context_window import current_window
    import copy
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
        prompt_version="facts-v5-dialogue-threads",
        status="warning",
        result_summary="Ожидает обработки с контекстом до 50 сообщений.",
        context_metadata={
            **({"context_window": copy.deepcopy(current_window())} if current_window() is not None else {}),
            "analysis_policy": "history-packets-v1",
            "full_history_policy": "thread-context-v2",
            "analysis_limits": {"analysis_input_token_limit":cfg.analysis_input_token_limit,
                                "analysis_output_token_limit":cfg.analysis_output_token_limit,
                                **{name:getattr(cfg,name) for name in ANALYSIS_CONFIG_FIELDS}},
            "schema_version": 1,
            "policy_version": POLICY,
            "source": "chat_history",
            "request_state": "not_sent",
            "snapshot_max_id": snapshot_max_id if snapshot_max_id is not None else
            (RawMessage.objects.aggregate(last=Max("id"))["last"] or raw.id),
        },
    )


def schedule_reanalysis(raw_id, user, request_key, *, recovery_id=None, expected_trace_id=None):
    with transaction.atomic():
        from .dialogue_threads import lock_source
        source = RawMessage.objects.select_related("config__team", "team", "project").get(pk=raw_id)
        lock_source(source)
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
        if recovery_id is not None and raw.traces.order_by('-id').values_list('pk', flat=True).first() != expected_trace_id:
            raise ProviderUnavailable('reanalysis_source_changed')
        active = OutboxEvent.objects.filter(
            event_type="extract_message",
            state__in=["pending", "enqueued", "processing"],
        ).filter(Q(payload__raw_id=raw.id) | Q(payload__batch_ids__contains=[raw.id])).first()
        if active:
            trace_id = next((entry["trace_id"] for entry in active.payload.get("trace_ids", [])
                             if entry["raw_id"] == raw.id), None)
            if trace_id is None and active.payload.get("raw_id") == raw.id:
                trace_id = active.payload.get("trace_id")
            if trace_id:
                return raw.traces.get(pk=trace_id)
            raise ProviderUnavailable("context_processing_in_progress")
        trace = reserve_attempt(raw, operation_key)
        if recovery_id is not None:
            trace.context_metadata['analysis_limits'].update(
                maintenance_quiet=True,
                error_recovery_operation_id=recovery_id,
            )
            trace.save(update_fields=['context_metadata'])
            if raw.processing_state == 'failed':
                raw.processing_state = 'received'
                raw.save(update_fields=['processing_state', 'updated_at'])
        from .live_batching import BUFFER_EVENT
        OutboxEvent.objects.filter(event_type=BUFFER_EVENT, payload__raw_id=raw.id, state="pending").update(
            state="done", payload={"raw_id": raw.id, "manual_trace_id": trace.id})
        if recovery_id is None:
            _enqueue_reanalysis(raw, trace, user, operation_key)
        AuditEvent.objects.create(
            actor=user,
            target_type="MessageProcessingTrace",
            target_id=trace.id,
            action="reanalysis_requested",
            before_after={"raw_message_id": raw.id, "attempt_no": trace.attempt_no},
        )
        return trace


def _enqueue_reanalysis(raw, trace, user, operation_key):
    from .batch_screening import enqueue
    enqueue([raw], operation_key, downstream={
        "raw_id": raw.pk, "trace_id": trace.pk, "requested_by_id": user.pk})
