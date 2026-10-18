"""Read API новой оболочки Messenger (спецификация §7, §8).

Все выборки идут через серверные границы access.py; каждый узел цепочки
фильтруется отдельно. Контракт списков: items, next_cursor, as_of, coverage
без конверта success/data. Статусы — машинный код, текст локализует UI.
"""
import base64
import binascii
import json
import re
from .communication import sanitize_body, sanitize_data
from urllib.parse import urlencode

from django.db import connection
from django.db.models import BigIntegerField, BooleanField, Count, Exists, F, JSONField, Max, OuterRef, Q, Subquery, Window
from django.db.models.functions import RowNumber
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import ValidationError
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .models import (
    ClarificationReply,
    CommunicationEvent,
    CommunicationRecord,
    CrmDelivery,
    FactCandidate,
    FactDecision,
    FactEvent,
    FactClarification,
    MessageProcessingTrace,
    NotificationDelivery,
    OutboxEvent,
    SourceWorkItem,
)

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
    # All outer joins are single-valued FKs; authorization collections are
    # subqueries. The OR branches cannot duplicate CommunicationRecord rows.
    return qs.filter(
        Q(category="group", config__in=access.configs_for(user))
        | Q(recipient=user)
        | Q(raw_message__in=access.messages_for(user).exclude(source="waha"))
    )


def record_href(record, **extra):
    return '/messenger?' + urlencode({
        's': record.session_name, 'c': record.chat_id, 'cat': record.category,
        't': record.team_id or '', 'record': record.pk, **extra,
    })


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
        out.setdefault(getattr(row, key), row)
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
            state = trace.axis_request_state
            if state == 'not_sent':
                return 'queued'
            if state in ('prepared', 'sent'):
                return 'analyzing'
            return "no_crm_actions"
        return "analyzed"
    if work is not None:
        if work.state == "error" or work.error_code:
            return "error"
        if work.state == "pending":
            return "queued"
        if work.state in ("done", "completed"):
            return "analyzed"
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
            "pending": "queued", "enqueued": "queued", "processing": "sending", "done": "sent",
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
    states = {"confirmed" if d.state == "delivered" else d.state for d in deliveries}
    for code in ("unknown", "unavailable", "failed", "blocked", "partial", "sending", "accepted", "confirmed"):
        if code in states:
            return code
    return "queued"


def axes_for_records(records):
    """Пакетный статус-адаптер без N+1: record_id -> оси состояний."""
    raw_ids = [r.raw_message_id for r in records if r.raw_message_id]
    notif_ids = [r.notification_id for r in records if r.notification_id]
    otp_ids = [r.otp_outbox_id for r in records if r.otp_outbox_id]

    works = _latest_by(
        SourceWorkItem.objects.filter(raw_message_id__in=raw_ids).order_by("raw_message_id", "-id")
        .distinct('raw_message_id').only('id', 'raw_message_id', 'state', 'error_code'),
        "raw_message_id",
    )
    latest_traces = MessageProcessingTrace.objects.filter(raw_message_id__in=raw_ids).order_by(
        'raw_message_id', '-id').distinct('raw_message_id').values('id')
    traces = _latest_by(
        MessageProcessingTrace.objects.filter(pk__in=Subquery(latest_traces))
        .only('id','raw_message_id','status','pipeline_action')
        .annotate(axis_request_state=F('context_metadata__request_state')),
        "raw_message_id",
    )
    trace_ids = [t.id for t in traces.values()]

    clars_by_notif = {
        c.notification_id: c
        for c in FactClarification.objects.filter(notification_id__in=notif_ids)
        .only('id','notification_id','state').order_by('id')
    }
    trace_to_raw = {t.id: t.raw_message_id for t in traces.values()}
    clars_by_raw = {}
    for clar in FactClarification.objects.filter(candidate__trace_id__in=trace_ids).only('id','state').annotate(
            axis_trace_id=F('candidate__trace_id')).order_by('id'):
        raw_id = trace_to_raw.get(clar.axis_trace_id)
        if raw_id is not None:
            clars_by_raw.setdefault(raw_id, clar)

    deliveries = _latest_by(
        NotificationDelivery.objects.filter(notification_id__in=notif_ids)
        .only('id','notification_id','state').order_by("notification_id", "-attempt_no", "-id"),
        "notification_id",
    )
    otps = {o.pk: o for o in OutboxEvent.objects.filter(pk__in=otp_ids).only('id','state')}

    crm_by_trace = {}
    crm_rows = CrmDelivery.objects.filter(
        fact_event__decision__candidate__trace_id__in=trace_ids,
    ).only('id','state').annotate(axis_trace_id=F('fact_event__decision__candidate__trace_id')).order_by(
        "fact_event__decision__candidate__trace_id", "-id")
    for row in crm_rows:
        crm_by_trace.setdefault(row.axis_trace_id, []).append(row)

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


def _possible_attention(qs):
    """Broad candidates only; historical failures need canonical rechecking."""
    return qs.filter(
        Q(raw_message_id__in=MessageProcessingTrace.objects.filter(status='error').values('raw_message_id')) |
        Q(raw_message_id__in=SourceWorkItem.objects.filter(Q(state='error') | ~Q(error_code='')).values('raw_message_id')) |
        Q(raw_message_id__in=FactClarification.objects.filter(state__in=['awaiting','expired','unconfirmed']).values('candidate__trace__raw_message_id')) |
        Q(notification_id__in=FactClarification.objects.filter(state__in=['queued','awaiting','expired','unconfirmed']).values('notification_id')) |
        Q(notification_id__in=NotificationDelivery.objects.exclude(state__in=['queued','pending','sending','sent','delivered','ack']).values('notification_id')) |
        Q(otp_outbox_id__in=OutboxEvent.objects.exclude(state__in=['pending','enqueued','processing','done']).values('id')) |
        Q(raw_message_id__in=CrmDelivery.objects.filter(state__in=['failed','unknown','unavailable']).values('fact_event__decision__candidate__trace__raw_message_id'))
    )


def _attention_batches(qs):
    # Only source links and dialog keys are needed, never message bodies.
    candidates = _possible_attention(qs).only(
        'id', 'raw_message_id', 'notification_id', 'otp_outbox_id',
        'session_name', 'chat_id', 'category', 'team_id',
    ).order_by('id')
    batch = []
    for record in candidates.iterator(chunk_size=1000):
        batch.append(record)
        if len(batch) == 1000:
            yield batch, axes_for_records(batch)
            batch = []
    if batch:
        yield batch, axes_for_records(batch)


def _current_axes_cte(qs):
    """Build the scoped latest-state relation shared by filtering and analytics."""
    trace_fields = MessageProcessingTrace._meta
    work_fields = SourceWorkItem._meta
    clarification_fields = FactClarification._meta
    delivery_fields = NotificationDelivery._meta
    outbox_fields = OutboxEvent._meta
    crm_fields = CrmDelivery._meta
    fact_event_fields = FactEvent._meta
    decision_fields = FactDecision._meta
    candidate_fields = FactCandidate._meta
    trace_table = connection.ops.quote_name(trace_fields.db_table)
    work_table = connection.ops.quote_name(work_fields.db_table)
    clarification_table = connection.ops.quote_name(clarification_fields.db_table)
    delivery_table = connection.ops.quote_name(delivery_fields.db_table)
    outbox_table = connection.ops.quote_name(outbox_fields.db_table)
    crm_table = connection.ops.quote_name(crm_fields.db_table)
    fact_event_table = connection.ops.quote_name(fact_event_fields.db_table)
    decision_table = connection.ops.quote_name(decision_fields.db_table)
    candidate_table = connection.ops.quote_name(FactCandidate._meta.db_table)

    def column(model_fields, name):
        return connection.ops.quote_name(model_fields.get_field(name).column)

    scoped_sql, scoped_params = qs.order_by().values(
        'id', 'raw_message_id', 'notification_id', 'otp_outbox_id'
    ).query.sql_with_params()
    trace_raw_id = column(trace_fields, 'raw_message')
    trace_status = column(trace_fields, 'status')
    trace_action = column(trace_fields, 'pipeline_action')
    trace_metadata = column(trace_fields, 'context_metadata')
    work_raw_id = column(work_fields, 'raw_message')
    work_state = column(work_fields, 'state')
    work_error = column(work_fields, 'error_code')
    clarification_notification = column(clarification_fields, 'notification')
    clarification_candidate = column(clarification_fields, 'candidate')
    clarification_state = column(clarification_fields, 'state')
    delivery_notification = column(delivery_fields, 'notification')
    delivery_state = column(delivery_fields, 'state')
    delivery_attempt = column(delivery_fields, 'attempt_no')
    outbox_state = column(outbox_fields, 'state')
    crm_fact_event = column(crm_fields, 'fact_event')
    crm_state = column(crm_fields, 'state')
    event_decision = column(fact_event_fields, 'decision')
    decision_candidate = column(decision_fields, 'candidate')
    candidate_trace = column(candidate_fields, 'trace')

    sql = f"""
        WITH scoped_records AS MATERIALIZED ({scoped_sql}),
        current_axes AS MATERIALIZED (
            SELECT scoped.id,
                CASE
                    WHEN COALESCE(notification_clar.state, trace_clar.state) = 'awaiting' THEN 'need_context'
                    WHEN latest_trace.id IS NOT NULL AND latest_trace.status = 'error' THEN 'error'
                    WHEN latest_trace.status IN ('sending', 'accepted', 'unknown') THEN 'analyzing'
                    WHEN latest_trace.pipeline_action = 'non_commercial'
                         AND latest_trace.request_state = 'not_sent' THEN 'queued'
                    WHEN latest_trace.pipeline_action = 'non_commercial'
                         AND latest_trace.request_state IN ('prepared', 'sent') THEN 'analyzing'
                    WHEN latest_trace.pipeline_action = 'non_commercial' THEN 'no_crm_actions'
                    WHEN latest_trace.id IS NOT NULL THEN 'analyzed'
                    WHEN latest_work.id IS NOT NULL
                         AND (latest_work.state = 'error' OR COALESCE(latest_work.error_code, '') <> '') THEN 'error'
                    WHEN latest_work.state = 'pending' THEN 'queued'
                    WHEN latest_work.state IN ('done', 'completed') THEN 'analyzed'
                    WHEN latest_work.id IS NOT NULL THEN 'analyzing'
                    ELSE 'received'
                END AS analysis,
                (
                    CASE
                        WHEN COALESCE(notification_clar.state, trace_clar.state) IN ('awaiting', 'expired', 'unconfirmed')
                            THEN TRUE
                        WHEN latest_trace.status = 'error' THEN TRUE
                        WHEN latest_trace.id IS NULL AND latest_work.id IS NOT NULL AND (
                            latest_work.state = 'error' OR COALESCE(latest_work.error_code, '') <> ''
                        ) THEN TRUE
                        WHEN latest_delivery.id IS NOT NULL AND latest_delivery.state NOT IN (
                            'queued', 'pending', 'sending', 'sent', 'delivered', 'ack'
                        ) THEN TRUE
                        WHEN latest_delivery.id IS NULL AND otp.id IS NOT NULL AND otp.state NOT IN (
                            'pending', 'enqueued', 'processing', 'done'
                        ) THEN TRUE
                        WHEN latest_trace.id IS NOT NULL AND EXISTS (
                            SELECT 1 FROM {crm_table} AS crm
                            INNER JOIN {fact_event_table} AS fact_event
                                ON fact_event.id = crm.{crm_fact_event}
                            INNER JOIN {decision_table} AS decision
                                ON decision.id = fact_event.{event_decision}
                            INNER JOIN {candidate_table} AS candidate
                                ON candidate.id = decision.{decision_candidate}
                            WHERE candidate.{candidate_trace} = latest_trace.id
                              AND crm.{crm_state} IN ('failed', 'unknown', 'unavailable')
                        ) THEN TRUE
                        ELSE FALSE
                    END
                ) AS has_attention
            FROM scoped_records AS scoped
            LEFT JOIN LATERAL (
                SELECT trace.id, trace.{trace_status} AS status,
                       trace.{trace_action} AS pipeline_action,
                       trace.{trace_metadata}->>'request_state' AS request_state
                FROM {trace_table} AS trace
                WHERE trace.{trace_raw_id} = scoped.raw_message_id
                ORDER BY trace.id DESC LIMIT 1
            ) AS latest_trace ON TRUE
            LEFT JOIN LATERAL (
                SELECT work.id, work.{work_state} AS state, work.{work_error} AS error_code
                FROM {work_table} AS work
                WHERE work.{work_raw_id} = scoped.raw_message_id
                ORDER BY work.id DESC LIMIT 1
            ) AS latest_work ON TRUE
            LEFT JOIN LATERAL (
                SELECT clarification.{clarification_state} AS state
                FROM {clarification_table} AS clarification
                WHERE clarification.{clarification_notification} = scoped.notification_id
                  AND scoped.notification_id IS NOT NULL
                ORDER BY clarification.id DESC LIMIT 1
            ) AS notification_clar ON TRUE
            LEFT JOIN LATERAL (
                SELECT clarification.{clarification_state} AS state
                FROM {clarification_table} AS clarification
                INNER JOIN {candidate_table} AS candidate
                    ON candidate.id = clarification.{clarification_candidate}
                WHERE candidate.trace_id = latest_trace.id
                ORDER BY clarification.id ASC LIMIT 1
            ) AS trace_clar ON TRUE
            LEFT JOIN LATERAL (
                SELECT delivery.id, delivery.{delivery_state} AS state
                FROM {delivery_table} AS delivery
                WHERE delivery.{delivery_notification} = scoped.notification_id
                  AND scoped.notification_id IS NOT NULL
                ORDER BY delivery.{delivery_attempt} DESC, delivery.id DESC LIMIT 1
            ) AS latest_delivery ON TRUE
            LEFT JOIN LATERAL (
                SELECT outbox.id, outbox.{outbox_state} AS state
                FROM {outbox_table} AS outbox
                WHERE outbox.id = scoped.otp_outbox_id
                LIMIT 1
            ) AS otp ON TRUE
        )
    """
    return sql, scoped_params


def _analysis_match_ids(qs, state=None, *, attention=False):
    """Filter states from one materialized latest-state relation in PostgreSQL."""
    if attention:
        # The canonical CTE is relatively expensive across a large cohort. Keep
        # its input to broad candidates; has_attention below still rechecks the
        # latest state, so stale failures cannot become false positives.
        qs = _possible_attention(qs)
    cte_sql, params = _current_axes_cte(qs)
    state_filter = "AND analysis = %s" if state else ""
    attention_filter = "AND has_attention" if attention else ""
    sql = f"{cte_sql} SELECT id FROM current_axes WHERE TRUE {state_filter} {attention_filter}"
    params = [*params]
    if state:
        params.append(state)
    return RawSQL(sql, params, output_field=BigIntegerField())


def aggregate_analysis_axes(qs):
    """Return complete per-analysis counts and attention count in one SQL query."""
    sql, params = _current_axes_cte(qs)
    sql += """
        SELECT analysis, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE has_attention) AS attention
        FROM current_axes
        GROUP BY analysis
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    states = {state: total for state, total, _attention in rows}
    attention_count = sum(attention for _state, _total, attention in rows)
    return states, attention_count


def filter_record_statuses(request, qs):
    """Drill down into the same message cohort, never into work-attempt states."""
    if request.query_params.get('cohort') == 'source':
        qs = qs.filter(direction='inbound',purpose='source')
    state = request.query_params.get('analysis')
    attention = request.query_params.get('attention') == '1'
    if not state and not attention:
        return qs
    if state and state not in ANALYSIS_LABELS:
        raise ValidationError({'error':'Неизвестное состояние анализа.'})
    if attention:
        return qs.filter(pk__in=_analysis_match_ids(qs, state, attention=True))
    if state:
        qs = qs.filter(pk__in=_analysis_match_ids(qs, state))
    return qs


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
        "sender_name": sanitize_body(record.raw_message.sender_name) if record.raw_message_id else '',
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
        return record.config.name if record.config_id else record.chat_id
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
                           | Q(config__name__icontains=q) | Q(raw_message__sender_name__icontains=q)
                           | Q(raw_message__sender_phone__icontains=q) | Q(sanitized_body__icontains=q))

        qs = filter_record_statuses(request, qs)
        grouped = qs.values("session_name", "chat_id", "category", "team_id").annotate(
            last_at=Max("occurred_at"), last_id=Max("id"))
        attention_only = request.query_params.get('attention') == '1'
        if attention_only:
            grouped = grouped.annotate(attention_count=Count('pk'))
        cursor = decode_cursor(request.query_params.get('cursor'))
        if cursor:
            if not isinstance(cursor, dict) or type(cursor.get('i')) is not int:
                raise ValidationError({'error':'Некорректный курсор диалогов.'})
            moment, ident = cursor.get('t'), cursor['i']
            if moment:
                grouped = grouped.filter(Q(last_at__lt=moment) | Q(last_at=moment,last_id__lt=ident) | Q(last_at__isnull=True))
            else:
                grouped = grouped.filter(last_at__isnull=True,last_id__lt=ident)
        limit = page_size(request, DIALOG_PAGE)
        dialogs = list(grouped.order_by(F('last_at').desc(nulls_last=True),'-last_id')[:limit+1])
        has_more = len(dialogs) > limit
        dialogs = dialogs[:limit]
        next_cursor = encode_cursor({'t':dialogs[-1]['last_at'],'i':dialogs[-1]['last_id']}) if has_more else None
        attention_counts = {}
        if attention_only:
            attention_counts = {_dialog_key(row): row['attention_count'] for row in dialogs}
        else:
            for rows, axes in _attention_batches(qs.filter(_dialog_filter(dialogs))):
                for record in rows:
                    if axes[record.pk]['attention']:
                        key = (record.session_name, record.chat_id, record.category, record.team_id)
                        attention_counts[key] = attention_counts.get(key, 0) + 1

        previews = {}
        if dialogs:
            ranked = qs.filter(_dialog_filter(dialogs)).annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("session_name"), F("chat_id"), F("category"), F("team_id")],
                    order_by=[F("occurred_at").desc(nulls_last=True), F("id").desc()],
                ),
            ).filter(rn=1).select_related("recipient","config")
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
                "match_record_id": preview.pk if q and preview is not None else None,
                "attention_count": attention_counts.get(key, 0),
            })
        if request.query_params.get("attention") == "1":
            items = [item for item in items if item["attention_count"] > 0]
        return Response({
            "items": items,
            "next_cursor": next_cursor,
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
            anchor = qs.get(pk=int(around))
        except (CommunicationRecord.DoesNotExist, TypeError, ValueError):
            return [], None
        half = max(1, limit // 2)
        if anchor.occurred_at is None:
            qs = qs.filter(occurred_at__isnull=True, id__lte=anchor.id)
            around = None
        else:
            before = qs.filter(Q(occurred_at__lt=anchor.occurred_at) | Q(
            occurred_at=anchor.occurred_at, id__lte=anchor.id) | Q(
            occurred_at__isnull=True)).order_by(*order)[:half]
            after = qs.filter(Q(occurred_at__gt=anchor.occurred_at) | Q(
                occurred_at=anchor.occurred_at, id__gt=anchor.id)).order_by('occurred_at', 'id')[:limit-half]
            ids = {row.pk for row in before} | {row.pk for row in after} | {anchor.pk}
            rows = list(qs.filter(pk__in=ids).order_by(*order))
            last = rows[-1]
            next_cursor = encode_cursor({'t': last.occurred_at.isoformat() if last.occurred_at else None, 'i': last.pk})
            return rows, next_cursor
    if cursor:
        moment, ident = cursor.get("t"), cursor.get("i")
        if moment and ident is not None:
            qs = qs.filter(Q(occurred_at__lt=moment) | Q(occurred_at=moment, id__lt=ident)
                           | Q(occurred_at__isnull=True))
        elif ident is not None:
            qs = qs.filter(occurred_at__isnull=True, id__lt=ident)
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
        raw_id = request.query_params.get("raw_message_id")
        if raw_id and raw_id.isdecimal():
            qs = qs.filter(raw_message_id=int(raw_id))
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

        qs = filter_record_statuses(request, qs)
        rows, next_cursor = _records_feed(request, qs.select_related('raw_message'))
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
        from .communication import sanitize_body
        payload['body'] = sanitize_body(raw.content, limit=None) if raw else record.sanitized_body
        payload.update({
            "origin": {
                "kind": "raw_message" if raw else ("notification" if record.notification_id else
                                                   "otp_outbox" if record.otp_outbox_id else "internal"),
                "raw_message_id": raw.pk if raw else None,
                "sender_name": sanitize_body(raw.sender_name) if raw else None,
                "sender_phone_masked": (raw.sender_phone[:3] + '•••' + raw.sender_phone[-2:]) if raw and re.fullmatch(r'\+?[0-9]{10,15}',raw.sender_phone) else None,
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
    if not record.raw_message_id:
        return False
    from .processing_attempts import require_reanalysis
    from .providers import ProviderUnavailable
    from rest_framework.exceptions import PermissionDenied
    try:
        require_reanalysis(user, record.raw_message)
    except (PermissionDenied, ProviderUnavailable):
        return False
    return True


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

        add_node("record", record.pk, "Сообщение", record.purpose, href=record_href(record))

        raw = record.raw_message
        if raw is not None:
            traces = list(MessageProcessingTrace.objects.filter(raw_message=raw).only('id','attempt_no','status').order_by("attempt_no"))
            can_trace = trace_allowed(request.user, record)
            candidates = {}
            for candidate in access.candidates_for(request.user).filter(trace_id__in=[t.pk for t in traces]).only('id','trace_id','status'):
                candidates.setdefault(candidate.trace_id, []).append(candidate)
            for trace in traces:
                add_node("trace", trace.pk, f"Попытка анализа №{trace.attempt_no}", trace.status,
                         allowed=can_trace,
                         href=record_href(record, tab='trace'))
                for candidate in candidates.get(trace.pk, []):
                    add_node("candidate", candidate.pk, "Предложенное изменение", candidate.status,
                             href=f"/analytics?view=review&candidate={candidate.pk}")
            questions = list(FactClarification.objects.filter(
                Q(candidate__trace__raw_message=raw) | Q(candidates__trace__raw_message=raw)
            ).distinct().only('id', 'notification_id', 'state').order_by('id'))
            replies = list(ClarificationReply.objects.filter(raw_message=raw)
                           .select_related('question').only(
                               'id', 'question_id', 'state', 'question__state', 'question__notification_id'
                           ).order_by('id'))
            notification_ids = {
                question.notification_id for question in questions if question.notification_id
            }
            notification_ids.update(
                reply.question.notification_id for reply in replies if reply.question.notification_id
            )
            related_records = scope.filter(notification_id__in=notification_ids).only(
                'id', 'notification_id', 'session_name', 'chat_id', 'category', 'team_id'
            ).order_by('id')
            record_by_notification = {}
            for target in related_records:
                record_by_notification.setdefault(target.notification_id, target)
            for question in questions:
                target = record_by_notification.get(question.notification_id)
                if target:
                    add_node("record", target.pk, "Личное уточнение", question.state, href=record_href(target))
            for reply in replies:
                target = record_by_notification.get(reply.question.notification_id)
                if target:
                    add_node("record", target.pk, "Вопрос к этому ответу", reply.question.state, href=record_href(target))
        if record.notification_id:
            clar = FactClarification.objects.filter(notification_id=record.notification_id).select_related(
                'candidate__trace'
            ).only('id', 'state', 'candidate__trace_id', 'candidate__trace__raw_message_id').first()
            if clar is not None:
                add_node("clarification", clar.pk, "Уточнение", clar.state)
                source_raw_id = clar.candidate.trace.raw_message_id if clar.candidate.trace_id else None
                target = None
                if source_raw_id:
                    target = scope.filter(raw_message_id=source_raw_id).only(
                        'id', 'raw_message_id', 'session_name', 'chat_id', 'category', 'team_id'
                    ).order_by('id').first()
                if target:
                    add_node("record", target.pk, "Исходное сообщение группы", "source", href=record_href(target))
                clarification_replies = ClarificationReply.objects.filter(question=clar).only(
                    'id', 'raw_message_id', 'state'
                ).order_by('id')
                reply_raw_ids = [reply.raw_message_id for reply in clarification_replies if reply.raw_message_id]
                reply_records = scope.filter(raw_message_id__in=reply_raw_ids).only(
                    'id', 'raw_message_id', 'session_name', 'chat_id', 'category', 'team_id'
                ).order_by('id')
                reply_record_by_raw_id = {}
                for reply_record in reply_records:
                    reply_record_by_raw_id.setdefault(reply_record.raw_message_id, reply_record)
                for reply in clarification_replies:
                    reply_record = reply_record_by_raw_id.get(reply.raw_message_id)
                    add_node("reply", reply.pk, "Ответ на уточнение", reply.state,
                             allowed=reply_record is not None,
                             href=record_href(reply_record) if reply_record else None)
        if record.notification_id:
            deliveries = list(NotificationDelivery.objects.filter(
                notification_id=record.notification_id).order_by("attempt_no"))
            for delivery in deliveries:
                add_node("delivery", delivery.pk, f"Попытка доставки №{delivery.attempt_no}", delivery.state)
        trace_row = MessageProcessingTrace.objects.filter(raw_message=raw).order_by("-id").only('id').first() if raw else None
        if trace_row is not None:
            crm_rows = CrmDelivery.objects.filter(
                fact_event__decision__candidate__trace=trace_row).select_related("external_object_link")
            for delivery in crm_rows:
                link = delivery.external_object_link
                add_node("crm_delivery", delivery.pk,
                         f"Изменение CRM ({link.object_type if link else 'объект'})", delivery.state)
        # События журнала дают хронологию и связи родитель→ребёнок.
        limit = page_size(request, CHAIN_PAGE_MAX)
        cursor = decode_cursor(request.query_params.get('cursor')) or {}
        events_query = CommunicationEvent.objects.filter(record=record).order_by('id')
        if type(cursor.get('i')) is int:
            events_query = events_query.filter(id__gt=cursor['i'])
        events = list(events_query[:limit+1])
        has_more = len(events) > limit
        events = events[:limit]
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
            "next_cursor": encode_cursor({'i':events[-1].pk}) if has_more else None,
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
        limit = page_size(request)
        cursor = decode_cursor(request.query_params.get('cursor')) or {}
        if raw:
            metadata_keys = [
                'source', 'request_state', 'analysis_policy', 'model', 'input_tokens_preflight',
                'provider_input_tokens', 'output_tokens', 'history_complete_in_request',
                'analysis_chunk_input_limit', 'max_completion_tokens', 'available_messages_count',
                'fully_included_messages_count', 'partial_messages_count', 'omitted_messages_count',
                'reduction_reason', 'retry', 'output_retry_generation', 'schema_repairs',
                'repair_reason', 'timings_ms', 'token_breakdown', 'completion_reserve_tokens',
                'input_tokens_actual', 'provider', 'api_format', 'schema_version', 'policy_version',
                'window_tokens', 'history_budget_tokens', 'full_history',
                'segment_diagnostics', 'response_diagnostics',
            ]
            traces = MessageProcessingTrace.objects.filter(raw_message=raw).only(
                'id', 'created_at', *SAFE_TRACE_FIELDS, 'ai_extracted_facts'
            ).annotate(
                _context_metadata_projection=RawSQL(
                    "CASE WHEN jsonb_typeof(context_metadata) = 'object' THEN "
                    "(SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) "
                    "FROM jsonb_each(context_metadata) WHERE key = ANY(%s)) "
                    "ELSE '{}'::jsonb END",
                    [metadata_keys], output_field=JSONField(),
                ),
                _has_context=RawSQL(
                    "CASE WHEN jsonb_typeof(earlier_messages_context) = 'array' "
                    "THEN jsonb_array_length(earlier_messages_context) > 0 ELSE FALSE END",
                    [], output_field=BooleanField(),
                ),
                _has_raw_payload=RawSQL(
                    "CASE WHEN jsonb_typeof(whatsapp_raw_payload) = 'object' "
                    "THEN jsonb_object_length(whatsapp_raw_payload) > 0 "
                    "WHEN jsonb_typeof(whatsapp_raw_payload) = 'array' "
                    "THEN jsonb_array_length(whatsapp_raw_payload) > 0 ELSE FALSE END",
                    [], output_field=BooleanField(),
                ),
            ).order_by('-id')
            if type(cursor.get('i')) is int:
                traces = traces.filter(pk__lt=cursor['i'])
            traces = list(traces[:limit+1])
        has_more = len(traces) > limit
        traces = traces[:limit]
        for trace in traces:
            row = {"id": trace.pk, "created_at": trace.created_at.isoformat() if trace.created_at else None}
            for field in SAFE_TRACE_FIELDS:
                value = getattr(trace, field, None)
                row[field] = str(value) if hasattr(value, "as_text") or field == "bitrix_deal_stage" else value
            row["ai_extracted_facts"] = sanitize_data(trace.ai_extracted_facts)
            metadata = trace._context_metadata_projection or {}
            row['context_metadata'] = sanitize_data({key: metadata[key] for key in (
                'source', 'request_state', 'analysis_policy', 'model', 'input_tokens_preflight',
                'provider_input_tokens', 'output_tokens', 'history_complete_in_request',
                'analysis_chunk_input_limit', 'max_completion_tokens', 'available_messages_count',
                'fully_included_messages_count', 'partial_messages_count', 'omitted_messages_count',
                'reduction_reason', 'retry',
                'output_retry_generation', 'schema_repairs', 'repair_reason',
                'timings_ms', 'token_breakdown', 'completion_reserve_tokens',
                'input_tokens_actual', 'provider', 'api_format', 'schema_version',
                'policy_version', 'window_tokens', 'history_budget_tokens',
            ) if key in metadata})
            if isinstance(metadata.get('full_history'), dict):
                row['context_metadata']['full_history'] = {key: value for key, value in metadata['full_history'].items()
                    if key in ('total','read','validated_read','phase','complete','stage','reconciliation_page','reconciliation_pages')}
            for name in ('segment_diagnostics','response_diagnostics'):
                if isinstance(metadata.get(name),dict):
                    row['context_metadata'][name] = sanitize_data({key:value for key,value in metadata[name].items()
                        if key in ('finish_reason','output_tokens','response_characters','provider_stop_reason','reasoning_characters')})
            row['has_context'] = trace._has_context
            row["has_raw_payload"] = trace._has_raw_payload
            items.append(sanitize_data(row))
        return Response({"items": items, "next_cursor": encode_cursor({"i": traces[-1].pk}) if has_more else None,
                         "as_of": timezone.now().isoformat(), "coverage": "scoped"})


class RecordTraceContextView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, trace_pk):
        from django.shortcuts import get_object_or_404
        from rest_framework.exceptions import PermissionDenied
        from .communication import sanitize_body
        record = _get_record(request, pk)
        if record is None:
            return Response({'error':'Запись недоступна.'},status=404)
        if not trace_allowed(request.user, record):
            raise PermissionDenied('Нет права на диагностику ИИ.')
        trace = get_object_or_404(
            MessageProcessingTrace.objects.only('id', 'raw_message_id'),
            pk=trace_pk, raw_message_id=record.raw_message_id,
        )
        table = connection.ops.quote_name(MessageProcessingTrace._meta.db_table)
        pk_column = connection.ops.quote_name(MessageProcessingTrace._meta.pk.column)
        context_column = connection.ops.quote_name(
            MessageProcessingTrace._meta.get_field('earlier_messages_context').column
        )
        # Read only the raw IDs needed to enforce per-message access. The large
        # historical JSON array stays inside PostgreSQL until its requested page.
        with connection.cursor() as db_cursor:
            db_cursor.execute(
                f"SELECT element.ordinality, (element.value->>'raw_message_id')::bigint "
                f"FROM {table} AS trace CROSS JOIN LATERAL "
                f"jsonb_array_elements(CASE WHEN jsonb_typeof(trace.{context_column}) = 'array' "
                f"THEN trace.{context_column} ELSE '[]'::jsonb END) WITH ORDINALITY "
                f"AS element(value, ordinality) WHERE trace.{pk_column} = %s "
                "AND jsonb_typeof(element.value) = 'object' "
                "AND jsonb_typeof(element.value->'raw_message_id') = 'number' "
                "AND (element.value->>'raw_message_id') ~ '^(0|[1-9][0-9]*)$' "
                "ORDER BY element.ordinality",
                [trace.pk],
            )
            snapshot_ids = list(db_cursor.fetchall())
        allowed = set(records_scope(request.user).filter(
            raw_message_id__in=[raw_id for _, raw_id in snapshot_ids]
        ).values_list('raw_message_id', flat=True))
        visible = [(ordinal, raw_id) for ordinal, raw_id in snapshot_ids if raw_id in allowed]
        cursor = decode_cursor(request.query_params.get('cursor')) or {}
        offset = cursor.get('o',0)
        if type(offset) is not int or offset < 0:
            return Response({'error':'Некорректный курсор.'},status=400)
        limit = min(page_size(request),25)
        page = visible[offset:offset+limit]
        page_ids = [raw_id for _, raw_id in page]
        records = {}
        for row in records_scope(request.user).filter(raw_message_id__in=page_ids).only(
            'id', 'raw_message_id', 'session_name', 'chat_id', 'category', 'team_id'
        ).order_by('id'):
            records.setdefault(row.raw_message_id, row)
        positions = [ordinal for ordinal, _ in page]
        snapshots_by_position = {}
        if positions:
            placeholders = ', '.join(['%s'] * len(positions))
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    f"SELECT element.ordinality, element.value FROM {table} AS trace "
                    f"CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(trace.{context_column}) = 'array' "
                    f"THEN trace.{context_column} ELSE '[]'::jsonb END) WITH ORDINALITY "
                    f"AS element(value, ordinality) WHERE trace.{pk_column} = %s "
                    f"AND element.ordinality IN ({placeholders}) ORDER BY element.ordinality",
                    [trace.pk, *positions],
                )
                snapshots_by_position = dict(db_cursor.fetchall())
        page_rows = []
        for ordinal, raw_id in page:
            snapshot = snapshots_by_position.get(ordinal)
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            if (isinstance(snapshot, dict)
                    and type(snapshot.get('raw_message_id')) is int
                    and snapshot['raw_message_id'] == raw_id):
                page_rows.append((snapshot, raw_id))
        items = []
        for row, raw_id in page_rows:
            body = row.get('content') if isinstance(row.get('content'),str) else row.get('text','')
            body = body if isinstance(body, str) else ''
            sender = row.get('sender_name')
            target = records.get(raw_id)
            items.append({'raw_message_id':raw_id, 'body':sanitize_body(body),
                'sender':sanitize_body(sender if isinstance(sender,str) else ''), 'time':row.get('timestamp'),
                'partial':bool(row.get('partial')) or len(body)>4000,
                'href':record_href(target) if target else None})
        return Response({'items':items, 'next_cursor':encode_cursor({'o':offset+limit}) if offset+limit < len(visible) else None,
            'as_of':timezone.now().isoformat(),'coverage':'scoped'})


# ---------------------------------------------------------------- changes

class ChangesView(APIView):
    """Дельта записей И событий после курсора. Два id-курсора: события к
    старым сообщениям тоже попадают в выдачу, а не только новые id лент."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        scope = records_scope(request.user)
        if not request.query_params.get("cursor"):
            # Establish a high-water mark before the initial feed load. Events
            # to old records after this snapshot still advance the event cursor.
            return Response({"items": [], "next_cursor": encode_cursor({
                "r": scope.aggregate(n=Max('id'))['n'] or 0,
                "e": CommunicationEvent.objects.filter(record__in=scope).aggregate(n=Max('id'))['n'] or 0,
            }), "as_of": timezone.now().isoformat(), "coverage": "scoped"})
        cursor = decode_cursor(request.query_params.get("cursor")) or {}
        try:
            after_record = int(cursor.get("r") or 0)
            after_event = int(cursor.get("e") or 0)
        except (TypeError, ValueError):
            after_record = after_event = 0
        limit = page_size(request)

        records = list(scope.filter(id__gt=after_record).select_related('raw_message').order_by("id")[:limit])
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
        from .processing_attempts import schedule_reanalysis, require_reanalysis
        from .providers import ProviderUnavailable
        from .mfa import require_privileged_mfa
        from rest_framework import serializers
        class Input(serializers.Serializer):
            idempotency_key = serializers.UUIDField()
        schema = Input(data=request.data)
        schema.is_valid(raise_exception=True)
        require_privileged_mfa(request)
        require_reanalysis(request.user, record.raw_message)
        try:
            trace = schedule_reanalysis(record.raw_message_id, request.user,
                str(schema.validated_data['idempotency_key']))
        except ProviderUnavailable:
            return Response({'error': 'Для сообщения уже выполняется анализ. Дождитесь результата.'}, status=409)
        event = OutboxEvent.objects.get(deduplication_key=trace.operation_key)
        return Response({'operation_id': event.pk, 'trace_id': trace.pk, 'state': event.state}, status=202)
