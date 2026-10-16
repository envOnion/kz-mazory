"""Communication journal — the only write path for CommunicationRecord/Event.

Every record and event is derived from an existing business row and keyed by a
deterministic ``deduplication_key``/``event_key``, so repeated webhooks, task
retries and history reconstruction cannot create duplicates.  Business state
stays canonical in the origin models; this module only projects and correlates.

Secrets (OTP codes, provider keys, bearer tokens) never enter the journal:
OTP records carry a fixed safe phrase, event metadata only lists codes and ids.
"""
from __future__ import annotations

import logging
import re

from django.db import IntegrityError, transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    ClarificationReply,
    CommunicationEvent,
    CommunicationRecord,
    CrmDelivery,
    FactClarification,
    MessageProcessingTrace,
    Notification,
    NotificationDelivery,
    OutboxEvent,
    RawMessage,
)

logger = logging.getLogger(__name__)

BODY_LIMIT = 4000
OTP_SAFE_PHRASE = "Отправлен код входа"

_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9._\-]{5,}"),  # JWT-like
    re.compile(r"(?i)\b(api[_\-]?key|token|secret|password)\b(\s*[:=]\s*)\S+"),
    re.compile(r"\b[0-9a-f]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
]


def sanitize_body(text: str) -> str:
    """Server-side redaction applied before anything is stored or displayed."""
    if not text:
        return ""
    body = text[:BODY_LIMIT]
    for pattern in _SECRET_PATTERNS:
        body = pattern.sub("[скрыто]", body)
    return body


def _safe_metadata(meta: dict) -> dict:
    clean = {}
    for key, value in meta.items():
        if isinstance(value, (int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value[:120] if re.fullmatch(r"[A-Za-z0-9 .:_\-\u0400-\u04FF/#+]{0,200}", value) else "[скрыто]"
    return clean


def add_event(record, *, kind, state, event_key, occurred_at=None, time_is_estimated=False,
              parent_event=None, safe_metadata=None, **links):
    if isinstance(event_key, tuple):
        event_key = ":".join(str(part) for part in event_key)
    defaults = {
        "record": record,
        "kind": kind,
        "state": state,
        "occurred_at": occurred_at or timezone.now(),
        "time_is_estimated": time_is_estimated,
        "parent_event": parent_event,
        "safe_metadata": _safe_metadata(safe_metadata or {}),
    }
    for name, value in links.items():
        if value is not None:
            defaults[name] = value
    try:
        with transaction.atomic():
            event, _ = CommunicationEvent.objects.get_or_create(event_key=event_key[:191], defaults=defaults)
    except IntegrityError:
        event = None
    return event


def get_or_create_record(deduplication_key, **fields):
    try:
        with transaction.atomic():
            record, created = CommunicationRecord.objects.get_or_create(deduplication_key=deduplication_key[:191], defaults=fields)
    except IntegrityError:
        record = CommunicationRecord.objects.filter(deduplication_key=deduplication_key[:191]).first()
        created = False
    return record, created


def category_for_raw(raw: RawMessage):
    """Group vs private vs system from the transport identity, never from text."""
    if raw.source == "bitrix":
        return "system", "internal", "system"
    if raw.processing_state == "clarification_reply":
        return "private", "inbound", "source"
    config = raw.config
    if config is not None and raw.chat_id and raw.chat_id == config.group_jid:
        return "group", "inbound", "source"
    return "private", "inbound", "source"


def journal_raw(raw: RawMessage):
    """Record one inbound row plus its ``received`` event. Idempotent."""
    category, direction, _purpose = category_for_raw(raw)
    known_time = bool(raw.timestamp and raw.sent_at_known)
    record, _ = get_or_create_record(
        f"raw:{raw.pk}",
        raw_message=raw,
        team_id=raw.team_id or (raw.config.team_id if raw.config_id else None),
        config_id=raw.config_id,
        session_name=raw.session_name,
        chat_id=raw.chat_id,
        category=category,
        direction=direction,
        purpose="source",
        sanitized_body=sanitize_body(raw.content),
        occurred_at=raw.timestamp if known_time else None,
        time_is_estimated=False,
    )
    if record is not None:
        add_event(record, kind="received", state=raw.processing_state,
                  event_key=f"raw:{raw.pk}:received",
                  occurred_at=raw.timestamp if known_time else timezone.now(),
                  time_is_estimated=not known_time,
                  safe_metadata={"source": raw.source})
    return record


def journal_raw_bulk(raws):
    """Batch variant used by history imports (bulk_create skips signals)."""
    existing = dict(CommunicationRecord.objects.filter(raw_message__in=[r.pk for r in raws])
                    .values_list("raw_message_id", "pk"))
    missing = [r for r in raws if r.pk not in existing]
    if not missing:
        return
    rows = []
    keys = {}
    for raw in missing:
        category, direction, _ = category_for_raw(raw)
        record = CommunicationRecord(
            raw_message=raw,
            team_id=raw.team_id or (raw.config.team_id if raw.config_id else None),
            config_id=raw.config_id,
            session_name=raw.session_name,
            chat_id=raw.chat_id,
            category=category, direction=direction, purpose="source",
            sanitized_body=sanitize_body(raw.content),
            occurred_at=raw.timestamp if raw.timestamp and raw.sent_at_known else None,
            deduplication_key=f"raw:{raw.pk}",
        )
        rows.append(record)
        keys[raw.pk] = f"raw:{raw.pk}"
    CommunicationRecord.objects.bulk_create(rows, ignore_conflicts=True, batch_size=250)
    saved = dict(CommunicationRecord.objects.filter(deduplication_key__in=list(keys.values())).values_list("deduplication_key", "pk"))
    events = []
    for raw in missing:
        record_id = saved.get(keys[raw.pk])
        if record_id is None:
            continue
        known_time = bool(raw.timestamp and raw.sent_at_known)
        events.append(CommunicationEvent(
            record_id=record_id, kind="received", state=raw.processing_state,
            event_key=f"raw:{raw.pk}:received",
            occurred_at=raw.timestamp if known_time else timezone.now(),
            time_is_estimated=not known_time, safe_metadata={"source": raw.source},
        ))
    if events:
        CommunicationEvent.objects.bulk_create(events, ignore_conflicts=True, batch_size=250)


def record_for_notification(notification):
    return CommunicationRecord.objects.filter(notification_id=notification.pk).first()


# ---------------------------------------------------------------- signals ---

@receiver(post_save, sender=RawMessage)
def on_raw_created(sender, instance, created, **kwargs):
    if created:
        journal_raw(instance)


def journal_notification(notification):
    purpose = "clarification" if notification.category == "clarification" else "notification"
    record, _ = get_or_create_record(
        f"notification:{notification.pk}",
        notification=notification,
        recipient=notification.recipient,
        category="private", direction="outbound", purpose=purpose,
        sanitized_body=sanitize_body(f"{notification.title}. {notification.message}"[:BODY_LIMIT]),
        occurred_at=notification.created_at or timezone.now(),
    )
    if record is not None:
        add_event(record, kind="dispatch", state="queued", event_key=("notif", notification.pk, "queued"),
                  occurred_at=notification.created_at or timezone.now())
    return record


@receiver(post_save, sender=Notification)
def on_notification_created(sender, instance, created, **kwargs):
    if created:
        journal_notification(instance)


def journal_otp(outbox):
    user_id = (outbox.payload or {}).get("user_id")
    record, _ = get_or_create_record(
        f"otp:{outbox.pk}",
        otp_outbox=outbox,
        recipient_id=user_id,
        category="system", direction="outbound", purpose="auth",
        sanitized_body=OTP_SAFE_PHRASE,
        occurred_at=outbox.created_at or timezone.now(),
    )
    if record is not None:
        add_event(record, kind="dispatch", state="queued", event_key=("otp", outbox.pk, "queued"),
                  occurred_at=outbox.created_at or timezone.now(),
                  safe_metadata={"event_type": outbox.event_type})
    return record


@receiver(post_save, sender=OutboxEvent)
def on_outbox_created(sender, instance, created, **kwargs):
    if created and instance.event_type == "otp":
        journal_otp(instance)


def _capture_state(sender, instance, **kwargs):
    """pre_save: remember the stored state so post_save sees real transitions."""
    state_field = _STATE_FIELDS.get(sender)
    if state_field and instance.pk:
        try:
            instance._journal_prev_state = sender.objects.filter(pk=instance.pk).values_list(state_field, flat=True).first()
        except Exception:  # pragma: no cover - defensive
            instance._journal_prev_state = None


_STATE_FIELDS = {
    NotificationDelivery: "state",
    OutboxEvent: "state",
    FactClarification: "state",
    ClarificationReply: "state",
    MessageProcessingTrace: "status",
    CrmDelivery: "state",
}

_DELIVERY_KIND = {"sending": "dispatch", "sent": "sent", "delivered": "ack", "ack": "ack",
                  "failed": "delivery_failed", "unknown": "unknown_result"}


@receiver(pre_save, sender=NotificationDelivery)
@receiver(pre_save, sender=OutboxEvent)
@receiver(pre_save, sender=FactClarification)
@receiver(pre_save, sender=ClarificationReply)
@receiver(pre_save, sender=MessageProcessingTrace)
@receiver(pre_save, sender=CrmDelivery)
def capture(sender, instance, **kwargs):
    _capture_state(sender, instance, **kwargs)


@receiver(post_save, sender=NotificationDelivery)
def on_delivery_saved(sender, instance, created, **kwargs):
    if created:
        record = record_for_notification(instance.notification)
        if record is not None:
            add_event(record, kind="dispatch", state="queued",
                      event_key=("deliv", instance.pk, "queued", instance.attempt_no),
                      notification_delivery=instance,
                      safe_metadata={"channel": instance.channel, "attempt_no": instance.attempt_no})
        return
    previous = getattr(instance, "_journal_prev_state", None)
    if previous == instance.state:
        return
    kind = _DELIVERY_KIND.get(instance.state, "dispatch")
    record = record_for_notification(instance.notification)
    if record is None:
        return
    add_event(record, kind=kind, state=instance.state,
              event_key=("deliv", instance.pk, instance.state, instance.attempt_no),
              notification_delivery=instance,
              safe_metadata={"error_code": instance.error_code})


@receiver(post_save, sender=OutboxEvent)
def on_outbox_state(sender, instance, created, **kwargs):
    if created:
        return
    previous = getattr(instance, "_journal_prev_state", None)
    if previous == instance.state:
        return
    record = CommunicationRecord.objects.filter(otp_outbox_id=instance.pk).first()
    if record is not None:
        kind = {"done": "sent", "failed": "delivery_failed", "unknown": "unknown_result"}.get(instance.state, "dispatch")
        add_event(record, kind=kind, state=instance.state,
                  event_key=("outbox", instance.pk, instance.state, instance.attempt_count),
                  outbox_event=instance, safe_metadata={"event_type": instance.event_type})


@receiver(post_save, sender=FactClarification)
def on_clarification_saved(sender, instance, created, **kwargs):
    record = CommunicationRecord.objects.filter(notification_id=instance.notification_id).first() if instance.notification_id else None
    if record is None:
        return
    if created:
        parent = None
        candidate = instance.candidate
        trace = getattr(candidate, "trace", None) if candidate is not None else None
        if trace is not None and trace.raw_message_id:
            parent = CommunicationEvent.objects.filter(event_key=f"raw:{trace.raw_message_id}:received").first()
        add_event(record, kind="clarification_created", state=instance.state,
                  event_key=("clar", instance.pk, "created"),
                  clarification=instance, parent_event=parent,
                  safe_metadata={"kind": instance.kind})
        return
    previous = getattr(instance, "_journal_prev_state", None)
    if previous == instance.state:
        return
    add_event(record, kind="clarification_state", state=instance.state,
              event_key=("clar", instance.pk, instance.state),
              clarification=instance)


@receiver(post_save, sender=ClarificationReply)
def on_reply_saved(sender, instance, created, **kwargs):
    record = CommunicationRecord.objects.filter(notification_id=instance.question.notification_id).first() if instance.question.notification_id else None
    if record is None:
        return
    question_event = CommunicationEvent.objects.filter(clarification_id=instance.question_id, kind="clarification_created").first()
    if instance.state in ("accepted", "rejected"):
        add_event(record, kind="answer_interpreted", state=instance.state,
                  event_key=("reply", instance.pk, instance.state),
                  reply=instance, parent_event=question_event)
    elif created:
        add_event(record, kind="clarification_state", state=instance.state,
                  event_key=("reply", instance.pk, "created"),
                  reply=instance, parent_event=question_event)


@receiver(post_save, sender=MessageProcessingTrace)
def on_trace_saved(sender, instance, created, **kwargs):
    if not created:
        return
    record = CommunicationRecord.objects.filter(raw_message_id=instance.raw_message_id).first()
    if record is None:
        return
    kind = "analysis_error" if instance.status in ("error", "failed", "provider_error") else "analysis_finished"
    add_event(record, kind=kind, state=instance.status,
              event_key=("trace", instance.pk, instance.status, instance.attempt_no),
              trace=instance,
              parent_event=CommunicationEvent.objects.filter(event_key=f"raw:{instance.raw_message_id}:received").first(),
              safe_metadata={"model": getattr(instance, "model_id", None),
                             "error_code": getattr(instance, "error_code", "")})


@receiver(post_save, sender=CrmDelivery)
def on_crm_saved(sender, instance, created, **kwargs):
    previous = None if created else getattr(instance, "_journal_prev_state", None)
    if not created and previous == instance.state:
        return
    record = _record_for_crm(instance)
    if record is None:
        return
    add_event(record, kind="crm_state", state=instance.state,
              event_key=("crm", instance.pk, instance.state),
              crm_delivery=instance, outbox_event_id=instance.outbox_event_id)


def _record_for_crm(delivery):
    """Follow stable FK chain to the originating inbound record, never time guesses."""
    decision = getattr(delivery, "fact_event", None)
    raw_id = None
    if decision is not None:
        candidate = decision.decision.candidate if decision.decision_id else None
        trace = candidate.trace if candidate and candidate.trace_id else None
        if trace is not None:
            raw_id = trace.raw_message_id
    if raw_id is not None:
        return CommunicationRecord.objects.filter(raw_message_id=raw_id).first()
    return None
