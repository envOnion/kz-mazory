"""API раздела «Аналитика», «Очередь» и «Изменения CRM» (спецификация §5, §7).

Метрики считаются по каноническим источникам: сообщения — по журналу
коммуникаций (уникальные логические сообщения), CRM-операции — по уникальному
логическому действию (fact_event), а не по числу retry. Каждое значение имеет
as_of, coverage и честные null вместо вымышленного нуля.
"""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .messenger_api import (
    axes_for_records,
    decode_cursor,
    encode_cursor,
    page_size,
    records_scope,
)
from .models import (
    CommunicationRecord,
    CrmCatalogSync,
    CrmDelivery,
    MessageProcessingTrace,
    NotificationDelivery,
    OutboxEvent,
    SourceWorkItem,
)

AI_EVENT_TYPES = ("route_message", "extract_message", "decide_fact", "crm_match")
DELIVERY_EVENT_TYPES = ("notification", "otp")

# Состояния CrmDelivery (задача ставится воркером, readback меняет их):
CRM_STATE_LABELS = {
    "pending": "Запланировано", "queued": "В очереди", "sending": "Отправляется",
    "accepted": "Запрос принят", "confirmed": "Подтверждено",
    "failed": "Ошибка", "unknown": "Результат неизвестен",
    "unavailable": "Результат неизвестен",
}


def _period(request):
    def parse(name):
        raw = request.query_params.get(name)
        if not raw:
            return None
        try:
            return timezone.datetime.fromisoformat(raw)
        except ValueError:
            return None
    moment_from, moment_to = parse("from"), parse("to")
    if timezone.is_naive(moment_from) if moment_from else False:
        moment_from = timezone.make_aware(moment_from, timezone.get_current_timezone())
    if timezone.is_naive(moment_to) if moment_to else False:
        moment_to = timezone.make_aware(moment_to, timezone.get_current_timezone())
    return moment_from, moment_to


def bitrix_object_url(link):
    """Настоящая ссылка Bitrix24 появляется только при известном внешнем ID и
    настроенном адресе портала; иначе честный null, а не заглушка."""
    base = getattr(settings, "BITRIX_BASE_URL", None)
    if base and link is not None and link.external_id:
        tail = {
            "deal": f"crm/deal/details/{link.external_id}/",
            "company": f"crm/company/details/{link.external_id}/",
            "contact": f"crm/contact/details/{link.external_id}/",
            "task": f"tasks/task/view/{link.external_id}/",
        }.get(link.object_type)
        if tail:
            return base.rstrip("/") + "/" + tail
    return None


class AnalyticsOperationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        moment_from, moment_to = _period(request)
        scope = records_scope(request.user)
        team = request.query_params.get("team")
        if team and team.isdigit():
            scope = scope.filter(team_id=int(team))
        window = Q(occurred_at__gte=moment_from) if moment_from else Q()
        if moment_to:
            window &= Q(occurred_at__lt=moment_to)
        inbound = scope.filter(direction="inbound", purpose="source").filter(window)
        outbound = scope.filter(direction="outbound").filter(window)

        # Состояние когорты на as_of считается адаптером по ограниченному
        # набору записей окна (не по всем строкам сразу).
        cohort_ids = list(inbound.values_list("id", flat=True)[:5000])
        cohort = list(CommunicationRecord.objects.filter(pk__in=cohort_ids))
        axes = axes_for_records(cohort)
        message_states = {}
        attention_count = 0
        for axes_row in axes.values():
            code = axes_row["analysis"]
            message_states[code] = message_states.get(code, 0) + 1
            if axes_row["attention"]:
                attention_count += 1

        outbound_purposes = dict(
            outbound.values_list("purpose").annotate(n=Count("id")))

        crm_window = Q()
        if moment_from:
            crm_window &= Q(updated_at__gte=moment_from)
        if moment_to:
            crm_window &= Q(updated_at__lt=moment_to)
        crm_ops = CrmDelivery.objects.filter(fact_event__isnull=False).filter(crm_window)
        crm_by_state = dict(crm_ops.values_list("state").annotate(n=Count("fact_event", distinct=True)))
        crm_attempts_total = crm_ops.count()

        current_errors = {
            "analysis": MessageProcessingTrace.objects.filter(status="error").count(),
            "delivery": NotificationDelivery.objects.filter(state="failed").count(),
            "outbox": OutboxEvent.objects.filter(state="failed").count(),
            "crm": CrmDelivery.objects.filter(state__in=["failed", "unknown"]).count(),
        }
        failed_attempts = {
            "analysis_traces": MessageProcessingTrace.objects.filter(status="error").count(),
            "delivery_attempts": NotificationDelivery.objects.filter(state="failed").count(),
            "outbox_retries": OutboxEvent.objects.filter(attempt_count__gt=0).count(),
        }

        horizon_from = moment_from or (timezone.now() - timedelta(days=30))
        daily = list(
            scope.filter(direction="inbound", purpose="source", occurred_at__gte=horizon_from)
            .annotate(day=TruncDate("occurred_at"))
            .values("day").annotate(n=Count("id")).order_by("day")[-60:]
        )

        catalog = None
        if team and team.isdigit():
            sync = CrmCatalogSync.objects.filter(team_id=int(team)).first()
            if sync is not None:
                catalog = {
                    "last_success_at": sync.last_success_at.isoformat() if sync.last_success_at else None,
                    "imported_count": sync.imported_count,
                    "state": sync.state,
                    "coverage": "partial" if sync.state != "done" or not sync.last_success_at else "complete",
                }

        return Response({
            "as_of": timezone.now().isoformat(),
            "timezone": settings.TIME_ZONE,
            "coverage": "scoped",
            "filters": {"from": moment_from.isoformat() if moment_from else None,
                        "to": moment_to.isoformat() if moment_to else None,
                        "team": int(team) if team and team.isdigit() else None},
            "messages": {
                "inbound_total": len(cohort_ids),
                "by_state": message_states,
                "attention": attention_count,
                "outbound_by_purpose": outbound_purposes,
            },
            "crm": {
                "operations_by_state": crm_by_state,
                "logical_operations": sum(crm_by_state.values()),
                "attempts_total": crm_attempts_total,
                "note": "операции — уникальные логические действия, attempts — попытки доставки",
            },
            "errors": {"current": current_errors, "failed_attempts": failed_attempts},
            "dynamics": [{"day": row["day"].isoformat() if row["day"] else None,
                          "messages": row["n"]} for row in daily],
            "catalog": catalog,
        })


class ProcessingJobsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queue = request.query_params.get("queue") or "all"
        limit = page_size(request)
        cursor = decode_cursor(request.query_params.get("cursor")) or {}
        try:
            before_id = int(cursor.get("i") or 0)
        except (TypeError, ValueError):
            before_id = 0
        state_filter = request.query_params.get("state")
        now = timezone.now()
        items = []

        def apply_cursor(qs):
            return qs.filter(id__lt=before_id) if before_id else qs

        if queue in ("all", "ai"):
            works = apply_cursor(SourceWorkItem.objects.select_related("raw_message__config")).order_by("-id")
            if state_filter:
                works = works.filter(state=state_filter)
            for row in works[:limit]:
                stale = (row.state not in ("pending", "done", "completed", "error")
                         and row.lease_until is not None and row.lease_until < now)
                items.append({
                    "queue": "ai", "id": row.id, "type": "source_work",
                    "state": "stale" if stale else row.state,
                    "raw_message_id": row.raw_message_id,
                    "processing_version": row.processing_version,
                    "error_code": row.error_code or None,
                    "lease_until": row.lease_until.isoformat() if row.lease_until else None,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                })
        if queue in ("all", "delivery", "crm", "history"):
            wanted = {
                "delivery": DELIVERY_EVENT_TYPES,
                "crm": ("crm_match", "decide_fact"),
                "history": ("whatsapp_history", "history_import"),
                "all": AI_EVENT_TYPES + DELIVERY_EVENT_TYPES
                       + ("whatsapp_history", "history_import"),
            }[queue]
            events = apply_cursor(OutboxEvent.objects.filter(event_type__in=wanted)).order_by("-id")
            if state_filter:
                events = events.filter(state=state_filter)
            for row in events[:limit]:
                tag = "delivery" if row.event_type in DELIVERY_EVENT_TYPES else (
                    "crm" if row.event_type in ("crm_match", "decide_fact") else "history")
                items.append({
                    "queue": tag, "id": row.id, "type": row.event_type,
                    "state": row.state,
                    "attempt_count": row.attempt_count,
                    "error_code": row.error_code or None,
                    "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
                    "lease_until": row.lease_until.isoformat() if row.lease_until else None,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                })
        items.sort(key=lambda row: row["updated_at"], reverse=True)
        items = items[:limit]
        next_cursor = None
        if items:
            next_cursor = encode_cursor({"i": min(row["id"] for row in items)})
        return Response({"items": items, "next_cursor": next_cursor,
                         "as_of": now.isoformat(), "coverage": "scoped"})


class CrmChangesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit = page_size(request)
        cursor = decode_cursor(request.query_params.get("cursor")) or {}
        try:
            before_id = int(cursor.get("i") or 0)
        except (TypeError, ValueError):
            before_id = 0
        qs = CrmDelivery.objects.select_related(
            "external_object_link", "fact_event__decision__candidate__team", "outbox_event")
        # Область доступа — по команде кандидата, не шире access.team_ids.
        if not request.user.is_superuser:
            teams = list(access.team_ids(request.user))
            qs = qs.filter(fact_event__decision__candidate__team_id__in=teams)
        object_type = request.query_params.get("object_type")
        if object_type in ("deal", "company", "contact", "task"):
            qs = qs.filter(external_object_link__object_type=object_type)
        state = request.query_params.get("state")
        if state:
            qs = qs.filter(state=state)
        moment_from, moment_to = _period(request)
        if moment_from:
            qs = qs.filter(updated_at__gte=moment_from)
        if moment_to:
            qs = qs.filter(updated_at__lt=moment_to)
        if before_id:
            qs = qs.filter(id__lt=before_id)

        rows = list(qs.order_by("-id")[:limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            link = row.external_object_link
            patch = row.patch or {}
            items.append({
                "id": row.id,
                "object_type": link.object_type if link else None,
                "external_id": link.external_id if link else None,
                "external_url": bitrix_object_url(link),
                "local_id": link.local_id if link else None,
                "action": row.fact_event.event_type if row.fact_event_id else None,
                "state": row.state,
                "error_code": row.error_code or None,
                "before": patch.get("before") if isinstance(patch, dict) else None,
                "after": patch.get("after") if isinstance(patch, dict) else None,
                "fact_event_id": row.fact_event_id,
                "outbox_event_id": row.outbox_event_id,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            })
        return Response({
            "items": items,
            "next_cursor": encode_cursor({"i": rows[-1].id}) if has_more and rows else None,
            "as_of": timezone.now().isoformat(),
            "coverage": "scoped",
            "state_labels": CRM_STATE_LABELS,
        })
