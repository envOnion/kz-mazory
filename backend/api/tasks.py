"""Only workers contact providers. PostgreSQL outbox survives Redis loss."""

import logging
import random
from datetime import timedelta
from urllib.parse import quote
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django_q.tasks import async_task
from . import access
from .models import (
    OutboxEvent,
    NotificationDelivery,
    RawMessage,
    WhatsAppConfig,
    AISettings,
)
from .providers import ProviderUnavailable
from .waha_control import ACTION_LABELS


class WahaOutcomeUnknown(Exception):
    pass


logger = logging.getLogger(__name__)
CLUSTERS = {
    "otp": "delivery",
    "notification": "delivery",
    "delivery_ack": "delivery",
    "crm_sync": "crm",
    "crm_import": "crm",
    "waha_control": "delivery",
    "history_import": "history",
}
NON_IDEMPOTENT = {"otp", "notification", "waha_control"}


def dispatch_outbox(limit=100):
    now = timezone.now()
    # A crash after a non-idempotent HTTP request has an unknown external outcome.
    expired = OutboxEvent.objects.filter(state="processing", lease_until__lte=now)
    for item in expired.filter(event_type__in=NON_IDEMPOTENT):
        with transaction.atomic():
            if OutboxEvent.objects.filter(
                pk=item.id, state="processing", lease_until__lte=now
            ).update(state="unknown", error_code="worker_interrupted"):
                if item.event_type == "notification":
                    NotificationDelivery.objects.filter(
                        pk=item.payload["delivery_id"], state="sending"
                    ).update(state="unknown", error_code="worker_interrupted")
    expired.exclude(event_type__in=NON_IDEMPOTENT).update(state="pending")
    OutboxEvent.objects.filter(state="enqueued", lease_until__lte=now).update(
        state="pending"
    )
    pending = OutboxEvent.objects.filter(state="pending", next_attempt_at__lte=now)
    if AISettings.get_active().message_processing_paused:
        pending = pending.exclude(event_type__in=["extract_message", "index_message"])
    ids = list(pending.order_by("id").values_list("id", flat=True)[:limit])
    for pk in ids:
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update().get(pk=pk)
            if event.state != "pending":
                continue
            event.state, event.lease_until = "enqueued", now + timedelta(minutes=5)
            event.save(update_fields=["state", "lease_until"])
        try:
            async_task(
                "api.tasks.run_outbox", pk, cluster=CLUSTERS.get(event.event_type, "ai")
            )
        except Exception:
            OutboxEvent.objects.filter(pk=pk, state="enqueued").update(
                state="pending", error_code="broker_unavailable"
            )
    return len(ids)


def run_outbox(pk):
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().get(pk=pk)
        if event.state not in ("pending", "enqueued"):
            return
        if (
            event.event_type in ("extract_message", "index_message")
            and AISettings.get_active().message_processing_paused
        ):
            event.state, event.lease_until = "pending", None
            event.next_attempt_at = timezone.now() + timedelta(seconds=10)
            event.save(update_fields=["state", "lease_until", "next_attempt_at"])
            return
        event.state, event.lease_until = (
            "processing",
            timezone.now()
            + timedelta(
                seconds=settings.AI_TASK_LEASE
                if CLUSTERS.get(event.event_type, "ai") in ("ai", "history")
                else 240
            ),
        )
        event.attempt_count += 1
        event.save()
    from .ai_service import usage_event_id

    context_token = usage_event_id.set(event.id)
    try:
        handlers = {
            "extract_message": extract_message,
            "index_message": index_message,
            "otp": deliver_otp,
            "notification": deliver_notification,
            "delivery_ack": apply_delivery_ack,
            "operation": run_operation,
            "crm_sync": sync_crm,
            "crm_import": import_crm,
            "attachment": process_attachment,
            "waha_control": waha_control,
            "history_import": import_history_step,
        }
        handlers[event.event_type](event.payload)
        OutboxEvent.objects.filter(pk=pk, state="processing").update(
            state="done", error_code="", lease_until=None
        )
    except WahaOutcomeUnknown:
        OutboxEvent.objects.filter(pk=pk).update(
            state="unknown", error_code="waha_outcome_unknown", lease_until=None
        )
        logger.warning("outbox_unknown id=%s type=%s", pk, event.event_type)
    except requests.Timeout:
        state = (
            "unknown"
            if event.event_type in NON_IDEMPOTENT or event.event_type == "crm_sync"
            else ("failed" if event.attempt_count >= 3 else "pending")
        )
        OutboxEvent.objects.filter(pk=pk).update(
            state=state,
            error_code="provider_timeout",
            next_attempt_at=timezone.now() + timedelta(minutes=2),
        )
        if event.event_type == "notification":
            NotificationDelivery.objects.filter(pk=event.payload["delivery_id"]).update(
                state="unknown", error_code="provider_timeout"
            )
        if event.event_type == "history_import" and state == "failed":
            from .history_jobs import fail_run

            fail_run(event.payload, "provider_timeout")
    except Exception as exc:
        code = (
            str(exc)[:64]
            if isinstance(exc, ProviderUnavailable)
            else type(exc).__name__
        )
        if code == "ai_daily_budget_exhausted":
            tomorrow = (timezone.now() + timedelta(days=1)).replace(
                hour=0, minute=0, second=1, microsecond=0
            )
            OutboxEvent.objects.filter(pk=pk).update(
                state="pending",
                error_code=code,
                next_attempt_at=tomorrow,
                lease_until=None,
                attempt_count=max(0, event.attempt_count - 1),
            )
            return
        from .history_jobs import PERMANENT_ERRORS

        permanent = (
            (
                code.startswith("context_")
                and code != "context_model_metadata_unavailable"
            )
            or code in PERMANENT_ERRORS
            or code
            in (
                "provider_context_overflow",
                "reanalysis_access_revoked",
                "provider_output_truncated",
                "invalid_extraction_schema",
                "invalid_schema",
                "evidence_not_in_source",
                "payment_evidence_contradiction",
                "context_request_uncertain",
            )
        )
        state = (
            "failed"
            if permanent
            or event.attempt_count >= 3
            or event.event_type in NON_IDEMPOTENT
            else "pending"
        )
        OutboxEvent.objects.filter(pk=pk).update(
            state=state,
            error_code=code,
            next_attempt_at=timezone.now()
            + timedelta(seconds=30 * 2**event.attempt_count + random.randint(0, 15)),
        )
        if event.event_type == "notification":
            NotificationDelivery.objects.filter(pk=event.payload["delivery_id"]).update(
                state="failed", error_code=code
            )
        if event.event_type == "history_import" and state == "failed":
            from .history_jobs import fail_run

            fail_run(event.payload, code)
        logger.warning(
            "outbox_failure id=%s type=%s code=%s", pk, event.event_type, code
        )

    finally:
        usage_event_id.reset(context_token)


def waha_request(method, path, data=None, timeout=15):
    if not settings.WAHA_API_KEY:
        raise ProviderUnavailable("waha_not_configured")
    response = requests.request(
        method,
        settings.WAHA_API_URL.rstrip("/") + path,
        json=data,
        headers={"X-Api-Key": settings.WAHA_API_KEY},
        timeout=timeout,
        allow_redirects=False,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def send_waha_whatsapp_message_task(phone_or_group, text, session="default"):
    chat_id = (
        phone_or_group
        if "@" in phone_or_group
        else "".join(x for x in phone_or_group if x.isdigit()) + "@c.us"
    )
    return waha_request(
        "POST", "/api/sendText", {"session": session, "chatId": chat_id, "text": text}
    )


def deliver_otp(payload):
    from .otp import delivery_code

    user = User.objects.filter(pk=payload["user_id"], is_active=True).first()
    if not user or not access.has_access(user, invited=True):
        return
    code = delivery_code(payload["delivery_id"])
    if not code:
        return
    send_waha_whatsapp_message_task(
        user.username, f"Код входа Mazory: {code}. Срок действия — 5 минут."
    )


def deliver_notification(payload):
    from .notifications import next_delivery_time, preferences

    delivery = NotificationDelivery.objects.select_related(
        "notification__recipient", "notification__commitment"
    ).get(pk=payload["delivery_id"])
    notification = delivery.notification
    user = notification.recipient

    def cancel():
        delivery.state = "cancelled"
        delivery.save(update_fields=["state"])

    if delivery.state in ("sent", "delivered", "unknown", "cancelled"):
        return
    if not access.has_access(user) or (
        notification.project_id
        and not access.projects_for(user, include_client=True)
        .filter(pk=notification.project_id)
        .exists()
    ):
        return cancel()
    if notification.commitment_id:
        commitment = notification.commitment
        if commitment.status not in (
            "pending",
            "overdue",
        ) or commitment.version != payload.get("commitment_version"):
            return cancel()
        if (
            not commitment.manager_id
            or not access.commitments_for(user).filter(pk=commitment.id).exists()
        ):
            return cancel()
        # Own reminder or current leader escalation; old assignee must never receive.
        if (
            user.id != commitment.manager.user_id
            and not access.memberships(user)
            .filter(team_id=commitment.project.team_id, role="team_lead")
            .exists()
        ):
            return cancel()
    prefs = preferences(user)
    if not prefs.get("whatsapp", True) or prefs.get(notification.category) is False:
        return cancel()
    send_at = next_delivery_time(user)
    if send_at > timezone.now() + timedelta(seconds=2):
        delivery.next_attempt_at = send_at
        delivery.state = "queued"
        delivery.save()
        OutboxEvent.objects.filter(
            payload__delivery_id=delivery.id,
            event_type="notification",
            state="processing",
        ).update(state="pending", next_attempt_at=send_at)
        return
    delivery.state = "sending"
    delivery.save(update_fields=["state"])
    result = send_waha_whatsapp_message_task(
        user.username, f"{notification.title}\n{notification.message}"
    )
    provider_id = result.get("id", "")
    if isinstance(provider_id, dict):
        provider_id = provider_id.get("_serialized", "")
    delivery.provider_message_id, delivery.state = str(provider_id)[:255], "sent"
    delivery.save(update_fields=["provider_message_id", "state", "updated_at"])


def apply_delivery_ack(payload):
    # A provider receipt can arrive before the send response; durable retry preserves it.
    if payload["session"] != "default" or payload["ack"] in (0, 1):
        return
    deliveries = NotificationDelivery.objects.filter(
        provider_message_id=payload["message_id"]
    ).exclude(provider_message_id="")
    if not deliveries.exists():
        raise ProviderUnavailable("delivery_receipt_waiting_for_send")
    if payload["ack"] >= 2:
        deliveries.filter(state__in=["sending", "sent", "unknown"]).update(
            state="delivered", error_code="", updated_at=timezone.now()
        )
    elif payload["ack"] == -1:
        deliveries.filter(state__in=["sending", "sent", "unknown"]).update(
            state="failed",
            error_code="provider_delivery_error",
            updated_at=timezone.now(),
        )


def extract_message(payload):
    from .pipeline import extract_message as extract

    extract(
        payload["raw_id"],
        trace_id=payload.get("trace_id"),
        requested_by_id=payload.get("requested_by_id"),
    )


def import_history_step(payload):
    from .history_jobs import ERROR_LABELS, process_step
    from .models import WhatsAppHistoryRun
    from .new_messages import monitor_error

    try:
        process_step(payload, waha_request)
    except (requests.RequestException, ProviderUnavailable) as exc:
        if isinstance(exc, requests.HTTPError):
            code = f"history_waha_http_{exc.response.status_code}"
        elif isinstance(exc, requests.Timeout):
            code = "provider_timeout"
        else:
            code = (
                str(exc)[:64]
                if isinstance(exc, ProviderUnavailable)
                else type(exc).__name__
            )
        if monitor_error(payload, code):
            return
        WhatsAppHistoryRun.objects.filter(
            pk=payload["history_run_id"], step=payload["step"]
        ).update(
            error_code=code,
            status_message=ERROR_LABELS.get(
                code, "WAHA временно недоступен. Шаг будет повторён."
            ),
            updated_at=timezone.now(),
        )
        raise ProviderUnavailable(code) from None


def index_message(payload):
    from .qdrant_service import qdrant_service

    raw = RawMessage.objects.select_related("config").get(pk=payload["raw_id"])
    if not raw.config_id or not raw.config.is_active:
        return
    point = qdrant_service.upsert_message(
        raw.message_id,
        raw.content,
        {
            "raw_message_id": raw.id,
            "message_id": raw.message_id,
            "config_id": raw.config_id,
            "content": raw.content,
            "sender_name": raw.sender_name,
            "sent_at": raw.timestamp.isoformat(),
            "sent_at_epoch": raw.timestamp.timestamp(),
        },
    )
    if point:
        RawMessage.objects.filter(pk=raw.id).update(qdrant_point_id=str(point))


def run_operation(payload):
    from .operations import execute_operation

    execute_operation(payload["operation_id"])


def sync_crm(payload):
    from .bitrix_service import BitrixService

    BitrixService.sync_project(payload["project_id"], payload["version"])


def import_crm(payload):
    from .bitrix_service import BitrixService

    BitrixService.propose_import(payload)


def process_attachment(payload):
    from .attachments import extract_attachment

    extract_attachment(payload["attachment_id"])


def waha_group_list(session):
    """NOWEB returns a JID-keyed map; other engines return an array."""
    groups, offset = {}, 0
    while True:
        result = waha_request(
            "GET",
            f"/api/{session}/groups?limit=100&offset={offset}&sortBy=id&sortOrder=asc&exclude=participants",
        )
        if isinstance(result, dict):
            result = list(result.values())
        if not isinstance(result, list):
            raise ProviderUnavailable("waha_invalid_groups")
        if not result:
            return sorted(
                groups.values(),
                key=lambda group: (group["name"].casefold(), group["id"]),
            )
        page = {}
        for item in result:
            if not isinstance(item, dict):
                raise ProviderUnavailable("waha_invalid_groups")
            jid = item.get("id")
            name = item.get("subject", item.get("name", ""))
            if (
                not isinstance(jid, str)
                or not jid.endswith("@g.us")
                or len(jid) > 128
                or not isinstance(name, str)
            ):
                raise ProviderUnavailable("waha_invalid_groups")
            page[jid] = {"id": jid, "name": name or jid}
        if not page.keys() - groups.keys():
            raise ProviderUnavailable("waha_repeated_groups")
        groups.update(page)
        # Short pages are not EOF: WAHA may clamp the requested page size.
        offset += len(result)


def waha_control(payload):
    cfg = WhatsAppConfig.objects.get(pk=payload["config_id"], is_active=True)
    action = payload["action"]
    if action not in ACTION_LABELS:
        raise ProviderUnavailable("waha_invalid_action")
    if payload.get("session_name", cfg.session_name) != cfg.session_name:
        raise ProviderUnavailable("waha_config_changed")
    session = quote(cfg.session_name, safe="")
    command = action in ("start", "restart", "stop", "logout")
    accepted = False

    def save_state(status, qr="", me=None, groups=None):
        cfg.status, cfg.last_qr_code = status, qr
        with transaction.atomic():
            for item in (
                WhatsAppConfig.objects.select_for_update()
                .filter(session_name=cfg.session_name)
                .order_by("id")
            ):
                item.status, item.last_qr_code = status, qr
                previous_me = item.snapshot.get("waha_me") or {}
                if me and previous_me.get("id") != me.get("id") or action == "logout":
                    item.snapshot.pop("waha_groups", None)
                item.snapshot = {**item.snapshot, "waha_me": me}
                if groups is not None:
                    item.snapshot["waha_groups"] = {
                        "session_name": cfg.session_name,
                        "account_id": (me or {}).get("id"),
                        "updated_at": timezone.now().isoformat(),
                        "items": groups,
                    }
                item.save(
                    update_fields=["status", "last_qr_code", "snapshot", "updated_at"]
                )

    try:
        if command:
            waha_request("POST", f"/api/sessions/{session}/{action}")
            accepted = True
            save_state("UNKNOWN")
        result = waha_request("GET", f"/api/sessions/{session}")
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(status, str) or not status or len(status) > 32:
            raise ProviderUnavailable("waha_invalid_response")
        me = result.get("me")
        if isinstance(me, dict) and status == "WORKING":
            me = {
                key: me[key]
                for key in ("id", "pushName")
                if isinstance(me.get(key), str)
            }
        else:
            me = None
        save_state(status, me=me)
        if action == "groups":
            if status != "WORKING" or not me or not me.get("id"):
                raise ProviderUnavailable("waha_groups_not_connected")
            groups = waha_group_list(session)
            if not WhatsAppConfig.objects.filter(
                pk=cfg.pk, is_active=True, session_name=cfg.session_name
            ).exists():
                raise ProviderUnavailable("waha_config_changed")
            save_state(status, me=me, groups=groups)
        if status == "SCAN_QR_CODE":
            result = waha_request("GET", f"/api/{session}/auth/qr?format=raw")
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ProviderUnavailable("waha_invalid_qr")
            save_state(status, qr=value)
    except (requests.Timeout, requests.ConnectionError) as exc:
        WhatsAppConfig.objects.filter(session_name=cfg.session_name).update(
            last_qr_code=""
        )
        if command:
            save_state("UNKNOWN")
            raise WahaOutcomeUnknown() from exc
        raise
    except Exception as exc:
        WhatsAppConfig.objects.filter(session_name=cfg.session_name).update(
            last_qr_code=""
        )
        # A confirmed command must not be retried because its subsequent read failed.
        if accepted or (
            command and not isinstance(exc, (requests.HTTPError, ProviderUnavailable))
        ):
            if not accepted:
                save_state("UNKNOWN")
            raise WahaOutcomeUnknown() from exc
        if isinstance(exc, requests.HTTPError):
            raise ProviderUnavailable(f"waha_http_{exc.response.status_code}") from exc
        raise
    logger.info(
        "waha_control_done config_id=%s action=%s status=%s", cfg.id, action, cfg.status
    )


def monitor_kpi_risks_and_anomalies_task():
    from .notifications import plan_reminders, plan_digests, plan_risks

    plan_reminders()
    plan_digests()
    plan_risks()


def enqueue_hourly_bitrix_sync_task():
    from .models import Project

    for p in Project.objects.filter(
        is_verified=True, needs_bitrix_sync=True, team__isnull=False
    ):
        OutboxEvent.objects.get_or_create(
            deduplication_key=f"crm:{p.id}:{p.version}",
            defaults={
                "event_type": "crm_sync",
                "payload": {"project_id": p.id, "version": p.version},
            },
        )


def sync_single_deal_to_bitrix_task(project_id):
    from .models import Project

    p = Project.objects.get(pk=project_id)
    return sync_crm({"project_id": p.id, "version": p.version})


create_bitrix_deal_task = sync_single_deal_to_bitrix_task


def process_incoming_message_task(message_data):
    # Legacy entry point accepts only a persisted source ID, never arbitrary webhook data.
    if not isinstance(message_data, int):
        raise ValueError("Persist source through authenticated inbox first")
    return extract_message({"raw_id": message_data})
