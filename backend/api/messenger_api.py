"""Read API новой оболочки Messenger (спецификация §7, §8).

Все выборки идут через серверные границы access.py; каждый узел цепочки
фильтруется отдельно. Контракт списков: items, next_cursor, as_of, coverage
без конверта success/data. Статусы — машинный код, текст локализует UI.
"""
import base64
import binascii
import json

from django.db import transaction
from django.db.models import Exists, F, Max, OuterRef, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .models import (
    AuditEvent,
    ClarificationReply,
    CommunicationEvent,
    CommunicationRecord,
    CrmDelivery,
    FactClarification,
    MessageProcessingTrace,
    NotificationDelivery,
    OutboxEvent,
    SourceWorkItem,
)
from .thread_routing import incoming_event_type

PAGE_DEFAULT = 50
PAGE_MAX = 100
CHAIN_PAGE_MAX = 200
DIALOG_PAGE = 200

# Терминальные «плохие» состояния событий и «улучшающие» состояния того же
# рода: старая ошибка не считается текущей, если позже был успешный переход.
BAD_EVENT_STATES = {
    "error", "failed", "unknown", "unavailable", "expired", "awaiting", "unconfirmed",
}
GOOD_EVENT_STATES = {
    "success", "done", "sent", "delivered", "ack", "confirmed", "resolved",
    "answered", "no_crm_actions", "analyzed",
}

ANALYSIS_LABELS = {
    "received": "Получено",
    "queued": "В очереди",
    "analyzing": "Анализируется",
    "need_context": "Нужен контекст",
    "analyzed": "Проанализировано",
    "no_crm_actions": "Без CRM-действий",
    "error": "Ошибка",
}
DELIVERY_LABELS = {
    "not_applicable": "Не требуется",
    "queued": "В очереди",
    "sending": "Отправляется",
    "sent": "Отправлено провайдеру",
    "delivered": "Доставлено",
    "failed": "Ошибка",
    "unknown": "Результат неизвестен",
}
CRM_LABELS = {
    "not_required": "Не требуется",
    "queued": "В очереди",
    "sending": "Отправляется",
    "accepted": "Запрос принят",
    "confirmed": "Подтверждено",
    "failed": "Ошибка",
    "unknown": "Результат неизвестен",
    "unavailable": "Результат неизвестен",
}


# ---------------------------------------------------------------- cursors

def encode_cursor(payload):
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def page_size(request, maximum=PAGE_MAX):
    try:
        limit = int(request.query_params.get("limit") or PAGE_DEFAULT)
    except (TypeError, ValueError):
        limit = PAGE_DEFAULT
    return max(1, min(limit, maximum))


# ------------------------------------------------------------------ scope

def records_scope(user):
    """Единая область видимости записей журнала; не шире access.py."""
    qs = CommunicationRecord.objects.all()
    if user.is_superuser:
        return qs
    if access.is_client(user):
        # Клиенту — только собственные личные записи (уведомления по своим проектам).
        return qs.filter(recipient=user)
    config_ids = list(access.configs_for(user).values_list("id", flat=True))
    teams = list(access.team_ids(user))
    return qs.filter(
        (Q(category="group", config_id__in=config_ids)
         | Q(team_id__in=teams)
         | Q(recipient=user))
    ).distinct()


def trace_allowed(user, record):
    if user.is_superuser or access.integration_allowed(user):
        return True
    team_id = record.team_id or (record.config.team_id if record.config_id else None)
    if team_id is None:
        return False
    return access.memberships(user).filter(
        team_id=team_id, role__in=["team_lead", "finance"]).exists()


# --------------------------------------------------------- status adapter

def _latest_by(rows, key):
    out = {}
    for row in rows:
        out.setdefault(row[key], row)
    return out


def _axis_from_trace(trace, work, clar_state):
    if clar_state == "awaiting":
        return "need_context"
    if trace is not None:
        if trace.status == "error":
            return "error"
        if trace.status in ("sending", "accepted", "unknown"):
            return "analyzing"
        if trace.pipeline_action == "non_commercial":
            return "no_crm_actions"
        return "analyzed"
    if work is not None:
        if work.state == "error" or work.error_code:
            return "error"
        if work.state == "pending":
            return "queued"
        return "analyzing"
    return "received"


def _delivery_axis(delivery, otp_outbox):
    if delivery is not None:
        return {
            "queued": "queued", "pending": "queued", "sending": "sending",
            "sent": "sent", "delivered": "delivered", "ack": "delivered",
            "failed": "failed", "error": "failed", "unknown": "unknown",
        }.get(delivery.state, "unknown")
    if otp_outbox is not None:
        return {
            "pending": "queued", "processing": "sending", "done": "delivered",
            "failed": "failed",
        }.get(otp_outbox.state, "unknown")
    return "not_applicable"


def _clarification_axis(clar, delivery_axis):
    if clar is None:
        return "not_applicable"
    mapping = {
        "queued": "preparing", "awaiting": "awaiting", "answered": "answered",
        "unconfirmed": "unconfirmed", "expired": "expired", "superseded": "superseded",
        "resolved": "resolved",
    }
    state = mapping.get(clar.state, "preparing")
    if state == "preparing" and delivery_axis in ("sending", "sent", "delivered"):
        state = "sending" if delivery_axis == "sending" else "awaiting"
    return state


def _crm_axis(deliveries):
    if not deliveries:
        return "not_required"
    states = {d.state for d in deliveries}
    for code in ("unknown", "unavailable", "failed", "sending", "accepted", "confirmed"):
        if code in states:
            return code
    return "queued"


def axes_for_records(records):
    """Пакетный статус-адаптер без N+1: record_id -> оси состояний."""
    raw_ids = [r.raw_message_id for r in records if r.raw_message_id]
    notif_ids = [r.notification_id for r in records if r.notification_id]
    otp_ids = [r.otp_outbox_id for r in records if r.otp_outbox_id]

    works = _latest_by(
        SourceWorkItem.objects.filter(raw_message_id__in=raw_ids).order_by("raw_message_id", "-id"),
        "raw_message_id",
    )
    traces = _latest_by(
        MessageProcessingTrace.objects.filter(raw_message_id__in=raw_ids).order_by("raw_message_id", "-id"),
        "raw_message_id",
    )
    trace_ids = [t.id for t in traces.values()]

    clars_by_notif = {
        c.notification_id: c
        for c in FactClarification.objects.filter(notification_id__in=notif_ids)
    }
    trace_to_raw = {t.id: t.raw_message_id for t in traces.values()}
    clars_by_raw = {}
    for clar in FactClarification.objects.filter(candidate__trace_id__in=trace_ids):
        raw_id = trace_to_raw.get(clar.candidate.trace_id)
        if raw_id is not None:
            clars_by_raw.setdefault(raw_id, clar)

    deliveries = _latest_by(
        NotificationDelivery.objects.filter(notification_id__in=notif_ids)
        .order_by("notification_id", "-attempt_no", "-id"),
        "notification_id",
    )
    otps = {o.pk: o for o in OutboxEvent.objects.filter(pk__in=otp_ids)}

    crm_by_trace = {}
    crm_rows = CrmDelivery.objects.filter(
        fact_event__decision__candidate__trace_id__in=trace_ids,
    ).select_related("fact_event__decision__candidate").order_by(
        "fact_event__decision__candidate__trace_id", "-id")
    for row in crm_rows:
        crm_by_trace.setdefault(row.fact_event.decision.candidate.trace_id, []).append(row)

    result = {}
    for record in records:
        trace = traces.get(record.raw_message_id) if record.raw_message_id else None
        work = works.get(record.raw_message_id) if record.raw_message_id else None
        clar = None
        if record.notification_id:
            clar = clars_by_notif.get(record.notification_id)
        if clar is None and record.raw_message_id:
            clar = clars_by_raw.get(record.raw_message_id)
        otp = otps.get(record.otp_outbox_id) if record.otp_outbox_id else None
        delivery = deliveries.get(record.notification_id) if record.notification_id else None
        analysis = _axis_from_trace(trace, work, clar.state if clar else None)
        delivery_axis = _delivery_axis(delivery, otp)
        clar_axis = _clarification_axis(clar, delivery_axis)
        crm_axis = _crm_axis(crm_by_trace.get(trace.id) if trace else [])
        reasons = []
        if analysis == "error":
            reasons.append("analysis_error")
        if delivery_axis in ("failed", "unknown"):
            reasons.append("delivery_problem")
        if crm_axis in ("failed", "unknown", "unavailable"):
            reasons.append("crm_problem")
        if clar_axis in ("awaiting", "expired", "unconfirmed"):
            reasons.append("clarification_" + clar_axis)
        result[record.pk] = {
            "analysis": analysis,
            "clarification": clar_axis,
            "delivery_whatsapp": delivery_axis,
            "crm": crm_axis,
            "attention": bool(reasons),
            "attention_reasons": reasons,
        }
    return result


def _active_bad_events():
    """Плохие события, для которых нет более нового успешного того же рода."""
    newer_good = CommunicationEvent.objects.filter(
        record_id=OuterRef("record_id"), kind=OuterRef("kind"),
        state__in=GOOD_EVENT_STATES, id__gt=OuterRef("id"),
    )
    return CommunicationEvent.objects.filter(state__in=BAD_EVENT_STATES).annotate(
        later_good=Exists(newer_good)).filter(later_good=False)


# ------------------------------------------------------------- serializing

def serialize_record_brief(record, axes):
    axis = axes.get(record.pk, {})
    when = record.occurred_at
    return {
        "id": record.id,
        "dialog": {
            "session_name": record.session_name,
            "chat_id": record.chat_id,
            "category": record.category,
            "team_id": record.team_id,
        },
        "category": record.category,
        "direction": record.direction,
        "purpose": record.purpose,
        "body": record.sanitized_body[:600],
        "occurred_at": when.isoformat() if when else None,
        "time_is_estimated": record.time_is_estimated,
        "statuses": axis,
        "attention": bool(axis.get("attention")),
    }


def _dialog_key(row):
    return (row["session_name"], row["chat_id"], row["category"], row["team_id"])


def _dialog_filter(dialogs):
    cond = Q(pk__in=[])
    for d in dialogs:
        cond |= Q(session_name=d["session_name"], chat_id=d["chat_id"],
                  category=d["category"], team_id=d["team_id"])
    return cond


def _name_for(record):
    if record.category == "group":
        return record.session_name or record.chat_id
    if record.recipient_id:
        return record.recipient.get_full_name() or record.recipient.username
    return "Система"


# ---------------------------------------------------------------- dialogs

class DialogsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = records_scope(request.user)
        category = request.query_params.get("category") or "all"
        if category in ("group", "private", "system"):
            qs = qs.filter(category=category)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(session_name__icontains=q) | Q(chat_id__icontains=q)
                           | Q(sanitized_body__icontains=q))

        dialogs = list(
            qs.values("session_name", "chat_id", "category", "team_id")
            .annotate(last_at=Max("occurred_at"))
            .order_by("-last_at")[:DIALOG_PAGE]
        )
        attention_counts = {}
        for row in (_active_bad_events()
                    .filter(record__in=qs)
                    .values("record__session_name", "record__chat_id", "record__category",
                            "record__team_id", "record_id")
                    .distinct()):
            key = (row["record__session_name"], row["record__chat_id"],
                   row["record__category"], row["record__team_id"])
            attention_counts[key] = attention_counts.get(key, 0) + 1

        previews = {}
        if dialogs:
            ranked = qs.filter(_dialog_filter(dialogs)).annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("session_name"), F("chat_id"), F("category"), F("team_id")],
                    order_by=[F("occurred_at").desc(nulls_last=True), F("id").desc()],
                ),
            ).filter(rn=1).select_related("recipient")
            for record in ranked:
                previews[(record.session_name, record.chat_id, record.category, record.team_id)] = record

        items = []
        for dialog in dialogs:
            key = _dialog_key(dialog)
            preview = previews.get(key)
            items.append({
                "key": "|".join("" if part is None else str(part) for part in key),
                "session_name": dialog["session_name"],
                "chat_id": dialog["chat_id"],
                "category": dialog["category"],
                "team_id": dialog["team_id"],
                "name": _name_for(preview) if preview is not None else (
                    dialog["session_name"] or dialog["chat_id"] or "Система"),
                "last_at": dialog["last_at"].isoformat() if dialog["last_at"] else None,
                "last_preview": preview.sanitized_body[:120] if preview is not None else "",
                "last_direction": preview.direction if preview is not None else None,
                "attention_count": attention_counts.get(key, 0),
            })
        if request.query_params.get("attention") == "1":
            items = [item for item in items if item["attention_count"] > 0]
        return Response({
            "items": items,
            "next_cursor": None,
            "as_of": timezone.now().isoformat(),
            "coverage": "scoped",
        })


# ---------------------------------------------------------------- records

ORDER_FIELD = "occurred_at"


def _records_feed(request, qs):
    """Лента (occurred_at, id) с nulls-last; курсор непрозрачный."""
    limit = page_size(request)
    cursor = decode_cursor(request.query_params.get("cursor"))
    around = request.query_params.get("around")
    order = [F("occurred_at").desc(nulls_last=True), F("id").desc()]
    if around:
        try:
            anchor = CommunicationRecord.objects.get(pk=int(around))
        except (CommunicationRecord.DoesNotExist, TypeError, ValueError):
            return [], None
        half = max(1, limit // 2)
        before = qs.filter(Q(occurred_at__lt=anchor.occurred_at) | Q(
            occurred_at=anchor.occurred_at, id__lte=anchor.id) | Q(
            occurred_at__isnull=True)).order_by(*order)[:half]
        after = qs.filter(occurred_at__gt=anchor.occurred_at).order_by(
            *order)[: limit - half]
        ids = sorted({row.pk for row in before} | {row.pk for row in after} | {anchor.pk})
        return list(qs.filter(pk__in=ids).order_by(*order)), None
    if cursor:
        moment, ident = cursor.get("t"), cursor.get("i")
        if moment and ident is not None:
            qs = qs.filter(Q(occurred_at__lt=moment) | Q(occurred_at=moment, id__lt=ident)
                           | Q(occurred_at__isnull=True))
        elif ident is not None:
            qs = qs.filter(id__lt=ident)
    rows = list(qs.order_by(*order)[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({
            "t": last.occurred_at.isoformat() if last.occurred_at else None,
            "i": last.id,
        })
    return rows, next_cursor


class RecordsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = records_scope(request.user)
        category = request.query_params.get("category")
        if category in ("group", "private", "system"):
            qs = qs.filter(category=category)
        if "session_name" in request.query_params:
            qs = qs.filter(session_name=request.query_params.get("session_name") or "")
        if "chat_id" in request.query_params:
            qs = qs.filter(chat_id=request.query_params.get("chat_id") or "")
        if request.query_params.get("team") is not None:
            team = request.query_params.get("team") or None
            qs = qs.filter(team_id=team if team and team.isdigit() else None)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(sanitized_body__icontains=q)

        rows, next_cursor = _records_feed(request, qs)
        axes = axes_for_records(rows) if rows else {}
        return Response({
            "items": [serialize_record_brief(row, axes) for row in rows],
            "next_cursor": next_cursor,
            "as_of": timezone.now().isoformat(),
            "coverage": "scoped",
        })


def _get_record(request, pk):
    try:
        record = CommunicationRecord.objects.select_related(
            "raw_message", "notification", "otp_outbox", "recipient", "config", "team").get(pk=pk)
    except CommunicationRecord.DoesNotExist:
        return None
    if not records_scope(request.user).filter(pk=record.pk).exists():
        return None
    return record


class RecordDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        record = _get_record(request, pk)
        if record is None:
            return Response({"error": "Запись недоступна."}, status=404)
        axes = axes_for_records([record])
        payload = serialize_record_brief(record, axes)
        raw = record.raw_message
        payload.update({
            "origin": {
                "kind": "raw_message" if raw else ("notification" if record.notification_id else
                                                   "otp_outbox" if record.otp_outbox_id else "internal"),
                "raw_message_id": raw.pk if raw else None,
                "sender_name": raw.sender_name if raw else None,
                "sender_phone_masked": None,
                "sent_at_known": raw.sent_at_known if raw else True,
                "source_time": raw.timestamp.isoformat() if raw else None,
                "event_type": record.otp_outbox.event_type if record.otp_outbox_id else None,
            },
            "capabilities": {
                "can_trace": trace_allowed(request.user, record),
                "can_reanalyze": bool(raw) and _can_reanalyze(request.user, record),
            },
            "counts": {
                "events": CommunicationEvent.objects.filter(record=record).count(),
            },
        })
        return Response(payload)


def _can_reanalyze(user, record):
    if record.raw_message_id is None:
        return False
    if user.is_superuser:
        return True
    team_id = record.team_id or (record.config.team_id if record.config_id else None)
    if team_id is None:
        return False
    return access.memberships(user).filter(team_id=team_id, role__in=["team_lead"]).exists()


class RecordChainView(APIView):
    """Разрешённый граф причин и следствий: узлы фильтруются по одному."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        record = _get_record(request, pk)
        if record is None:
            return Response({"error": "Запись недоступна."}, status=404)
        scope = records_scope(request.user)
        nodes, edges = [], []

        def add_node(kind, node_id, label, state, allowed=True, href=None, meta=None):
            node = {"kind": kind, "id": node_id, "label": label, "state": state,
                    "allowed": bool(allowed), "href": href, "meta": meta or {}}
            if allowed:
                nodes.append(node)
            return node

        add_node("record", record.pk, "Сообщение", record.purpose, href=f"/messenger?record={record.pk}")

        raw = record.raw_message
        if raw is not None:
            for trace in MessageProcessingTrace.objects.filter(raw_message=raw).order_by("attempt_no"):
                add_node("trace", trace.pk, f"Попытка анализа №{trace.attempt_no}", trace.status,
                         allowed=trace_allowed(request.user, record),
                         href=f"/messenger/records/{record.pk}/traces")
                for candidate in trace.candidates.all():
                    add_node("candidate", candidate.pk, "Предложенное изменение", candidate.status)
        if record.notification_id:
            clar = FactClarification.objects.filter(notification_id=record.notification_id).first()
            if clar is not None:
                add_node("clarification", clar.pk, "Уточнение", clar.state)
                for reply in ClarificationReply.objects.filter(question=clar).select_related("raw_message"):
                    reply_record = scope.filter(raw_message=reply.raw_message).first() if reply.raw_message_id else None
                    add_node("reply", reply.pk, "Ответ на уточнение", reply.state,
                             allowed=reply_record is not None,
                             href=f"/messenger?record={reply_record.pk}" if reply_record else None)
        if record.notification_id:
            deliveries = list(NotificationDelivery.objects.filter(
                notification_id=record.notification_id).order_by("attempt_no"))
            for delivery in deliveries:
                add_node("delivery", delivery.pk, f"Попытка доставки №{delivery.attempt_no}", delivery.state)
        trace_row = MessageProcessingTrace.objects.filter(raw_message=raw).order_by("-id").first() if raw else None
        if trace_row is not None:
            crm_rows = CrmDelivery.objects.filter(
                fact_event__decision__candidate__trace=trace_row).select_related("external_object_link")
            for delivery in crm_rows:
                link = delivery.external_object_link
                add_node("crm_delivery", delivery.pk,
                         f"Изменение CRM ({link.object_type if link else 'объект'})", delivery.state)
        # События журнала дают хронологию и связи родитель→ребёнок.
        limit = page_size(request, CHAIN_PAGE_MAX)
        events = list(CommunicationEvent.objects.filter(record=record).order_by("occurred_at", "id")[:limit])
        timeline = []
        for event in events:
            timeline.append({
                "id": event.pk,
                "kind": event.kind,
                "state": event.state,
                "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
                "time_is_estimated": event.time_is_estimated,
                "parent_event_id": event.parent_event_id,
            })
            edges.append(["root", f"event:{event.pk}"])
        return Response({
            "root_record": record.pk,
            "nodes": nodes,
            "edges": edges,
            "timeline": timeline,
            "next_cursor": None,
            "as_of": timezone.now().isoformat(),
            "coverage": "scoped",
        })


SAFE_TRACE_FIELDS = (
    "attempt_no", "status", "pipeline_action", "model_version", "prompt_version",
    "error_code", "result_summary", "ai_confidence", "earlier_messages_count",
    "bitrix_matched_deal_id", "bitrix_deal_title", "bitrix_deal_stage",
)


class RecordTracesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        record = _get_record(request, pk)
        if record is None:
            return Response({"error": "Запись недоступна."}, status=404)
        if not trace_allowed(request.user, record):
            return Response({"error": "Диагностика ИИ доступна руководителю команды."}, status=403)
        raw = record.raw_message
        traces = MessageProcessingTrace.objects.filter(raw_message=raw).order_by("attempt_no") if raw else []
        items = []
        for trace in traces:
            row = {"id": trace.pk, "created_at": trace.created_at.isoformat()}
            for field in SAFE_TRACE_FIELDS:
                value = getattr(trace, field, None)
                row[field] = str(value) if hasattr(value, "as_text") or field == "bitrix_deal_stage" else value
            row["ai_extracted_facts"] = trace.ai_extracted_facts
            row["context_metadata"] = trace.context_metadata
            row["has_raw_payload"] = bool(trace.whatsapp_raw_payload)
            items.append(row)
        return Response({"items": items, "next_cursor": None,
                         "as_of": timezone.now().isoformat(), "coverage": "scoped"})


# ---------------------------------------------------------------- changes

class ChangesView(APIView):
    """Дельта записей И событий после курсора. Два id-курсора: события к
    старым сообщениям тоже попадают в выдачу, а не только новые id лент."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        scope = records_scope(request.user)
        cursor = decode_cursor(request.query_params.get("cursor")) or {}
        try:
            after_record = int(cursor.get("r") or 0)
            after_event = int(cursor.get("e") or 0)
        except (TypeError, ValueError):
            after_record = after_event = 0
        limit = page_size(request)

        records = list(scope.filter(id__gt=after_record).order_by("id")[:limit])
        events = list(
            CommunicationEvent.objects.filter(record__in=scope, id__gt=after_event)
            .order_by("id")[:limit]
        )
        axes = axes_for_records(records) if records else {}
        items = [{"type": "record", **serialize_record_brief(row, axes)} for row in records]
        items += [{
            "type": "event",
            "id": event.pk,
            "record_id": event.record_id,
            "kind": event.kind,
            "state": event.state,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "time_is_estimated": event.time_is_estimated,
        } for event in events]
        return Response({
            "items": items,
            "next_cursor": encode_cursor({
                "r": max([r.pk for r in records] + [after_record]),
                "e": max([e.pk for e in events] + [after_event]),
            }) if (records or events) else request.query_params.get("cursor"),
            "as_of": timezone.now().isoformat(),
            "coverage": "scoped",
        })


# --------------------------------------------------------------- commands

class ReanalyzeView(APIView):
    """Явная команда повторного анализа: права, идемпотентный ключ, 202,
    прежние попытки сохраняются (новая попытка = новый outbox-событие)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        record = _get_record(request, pk)
        if record is None:
            return Response({"error": "Запись недоступна."}, status=404)
        if not _can_reanalyze(request.user, record):
            return Response({"error": "Недостаточно прав для повторного анализа."}, status=403)
        idempotency_key = (request.data.get("idempotency_key") or "").strip()
        if not idempotency_key:
            return Response({"error": "Требуется idempotency_key."}, status=400)
        raw = record.raw_message
        config = record.config
        event_type = incoming_event_type(config)
        deduplication_key = f"reanalyze:{raw.pk}:{idempotency_key}"[:191]
        with transaction.atomic():
            existing = OutboxEvent.objects.filter(deduplication_key=deduplication_key).first()
            if existing is None:
                existing = OutboxEvent.objects.create(
                    event_type=event_type,
                    payload={
                        "raw_message_id": raw.pk,
                        "config_id": config.pk if config else None,
                        "session_name": record.session_name,
                        "chat_id": record.chat_id,
                        "reanalyze": True,
                    },
                    deduplication_key=deduplication_key,
                )
            AuditEvent.objects.create(
                actor=request.user,
                target_type="communication_record",
                target_id=record.pk,
                action="messenger_reanalyze",
                before_after={"outbox_event_id": existing.pk, "event_type": event_type},
            )
        return Response({"operation_id": existing.pk, "state": existing.state}, status=202)
