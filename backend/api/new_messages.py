"""Continuous, checkpointed WhatsApp reads; each queue task does bounded work."""

import hashlib
import json
import math
from datetime import timedelta
from urllib.parse import quote, urlencode

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .history_jobs import (
    ERROR_LABELS,
    IMPORT_STATES,
    PERMANENT_ERRORS,
    control_run,
    enqueue_step,
    locked_run,
    persist_items,
    source,
    validate_page,
)
from .models import RawMessage, WhatsAppHistoryJob, WhatsAppHistoryRun
from .providers import ProviderUnavailable


def initialize_checkpoint(job, config):
    identity = source(config)
    if job.checkpoint_source == identity and job.new_messages_since:
        return
    now = timezone.now().replace(microsecond=0)
    latest = (
        RawMessage.objects.filter(
            config=config,
            source="waha",
            session_name=config.session_name,
            chat_id=config.group_jid,
            sent_at_known=True,
            timestamp__lte=now,
        )
        .filter(Q(team_id=config.team_id) | Q(team_id__isnull=True))
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    job.new_messages_since = job.new_messages_started_at = latest or now
    job.checkpoint_source, job.last_checked_at = identity, None
    job.save(
        update_fields=[
            "new_messages_since",
            "new_messages_started_at",
            "checkpoint_source",
            "last_checked_at",
        ]
    )


def sync_monitor_settings(job, changed, user):
    """Called inside the admin's transaction, with the job row already locked."""
    if not job.only_new:
        return
    if job.enabled:
        initialize_checkpoint(job, job.config)
    active = job.runs.filter(run_kind="monitor", state__in=(*IMPORT_STATES, "paused")).first()
    if not active or not active.settings_snapshot.get("only_new"):
        return
    if not job.enabled and active.state != "paused":
        control_run(active.pk, "pause", user)
    elif job.enabled and "enabled" in changed and active.state == "paused":
        control_run(active.pk, "resume", user)


def schedule_next(run, delay=0):
    when = timezone.now() + timedelta(seconds=delay)
    WhatsAppHistoryJob.objects.filter(pk=run.job_id).update(next_run_at=when)
    enqueue_step(run, delay)


def monitor_error(payload, code):
    """Retry transient provider failures forever; pause actionable source errors."""
    with transaction.atomic():
        initial = WhatsAppHistoryRun.objects.get(pk=payload["history_run_id"])
        if not initial.settings_snapshot.get("only_new"):
            return False
        job = WhatsAppHistoryJob.objects.select_for_update().get(pk=initial.job_id)
        run = WhatsAppHistoryRun.objects.select_for_update().get(pk=initial.pk)
        if run.step != payload["step"] or run.state not in IMPORT_STATES:
            return True
        run.error_code = code
        if code in PERMANENT_ERRORS - {"history_waha_http_422"} or not job.enabled:
            run.resume_state, run.state = run.state, "paused"
            run.status_message = ERROR_LABELS.get(
                code, "Исправьте настройки и продолжите мониторинг."
            )
            run.save()
            job.next_run_at = None
            job.save(update_fields=["next_run_at"])
        else:
            failures = min(run.incremental_state.get("failures", 0) + 1, 6)
            run.incremental_state["failures"] = failures
            delay = min(60, job.new_message_poll_seconds * 2 ** (failures - 1))
            run.status_message = f"WAHA временно недоступен. Повтор через {delay} сек.; прогресс сохранён."
            schedule_next(run, delay)
        return True


def process_new_messages(payload, waha):
    run_id, step = payload["history_run_id"], payload["step"]
    with transaction.atomic():
        run, config = locked_run(run_id, step)
        if run is None:
            return
        # Settings take effect between pages without resetting the checkpoint.
        run.settings_snapshot.update(
            analyze_after_import=run.job.analyze_after_import,
            new_message_poll_seconds=run.job.new_message_poll_seconds,
        )
        if run.state == "watching":
            run.state = "waiting_connection"
        if run.state == "waiting_connection":
            run.save()
        request_state = run.state
    session = quote(run.source_snapshot["session_name"], safe="")
    chat = quote(run.source_snapshot["chat_id"], safe="")
    if request_state == "waiting_connection":
        result = waha("GET", f"/api/sessions/{session}")
        if result.get("status") == "WORKING":
            storage = (result.get("config") or {}).get("noweb", {}).get("store", {})
            if not storage.get("enabled") or not storage.get("fullSync"):
                raise ProviderUnavailable("history_storage_disabled")
            group = waha("GET", f"/api/{session}/groups/{chat}")
            if group.get("id") != run.source_snapshot["chat_id"]:
                raise ProviderUnavailable("history_wrong_chat")
    elif request_state == "collecting":
        params = {
            "limit": run.settings_snapshot["page_size"],
            "offset": run.offset,
            "sortBy": "timestamp",
            "sortOrder": "asc",
            "downloadMedia": "false",
            "filter.timestamp.gte": run.incremental_state["since"],
            "filter.timestamp.lte": int(run.cutoff_at.timestamp()),
        }
        result = waha(
            "GET",
            f"/api/{session}/chats/{chat}/messages?{urlencode(params)}",
            timeout=60,
        )
    else:
        raise ProviderUnavailable("history_invalid_message")
    with transaction.atomic():
        run, config = locked_run(run_id, step)
        if run is None:
            return
        job = run.job
        run.settings_snapshot["analyze_after_import"] = job.analyze_after_import
        run.error_code = ""
        run.incremental_state.pop("failures", None)
        if request_state == "waiting_connection":
            status = result.get("status", "UNKNOWN")
            if status != "WORKING":
                run.state = "waiting_connection"
                run.status_message = f"WhatsApp: {status}. Ожидаем подключение; мониторинг продолжится автоматически."
                schedule_next(run, job.new_message_poll_seconds)
                return
            initialize_checkpoint(job, config)
            run.source_snapshot["group_title"] = str(group.get("subject") or "")[:255]
            lower = max(
                job.new_messages_started_at,
                job.new_messages_since - timedelta(seconds=60),
            )
            run.cutoff_at = max(
                job.new_messages_since, timezone.now().replace(microsecond=0)
            )
            run.incremental_state = {
                "since": int(lower.timestamp()),
                "page_signatures": [],
            }
            run.offset, run.state = 0, "collecting"
            run.status_message = "Проверяем новые сообщения."
            schedule_next(run)
            return
        if not isinstance(result, list):
            raise ProviderUnavailable("history_invalid_message")
        # Validate the entire provider response before persisting any page. Some
        # NOWEB releases do not combine timestamp filters; enforce both locally.
        lower, upper = run.incremental_state["since"], run.cutoff_at.timestamp()
        accepted, beyond = [], False
        for message in result:
            if not isinstance(message, dict) or type(message.get("timestamp")) not in (
                int,
                float,
            ):
                raise ProviderUnavailable("history_invalid_message")
            stamp = message["timestamp"]
            if not math.isfinite(stamp) or stamp <= 0:
                raise ProviderUnavailable("history_invalid_message")
            if (
                message.get("from") != run.source_snapshot["chat_id"]
                and message.get("to") != run.source_snapshot["chat_id"]
            ):
                raise ProviderUnavailable("history_wrong_chat")
            if stamp < lower:
                # Never silently fall back to scanning the whole history.
                raise ProviderUnavailable("history_incremental_filter_unsupported")
            if stamp > upper:
                beyond = True
            else:
                accepted.append(message)
        items = validate_page(accepted, run)
        if result:
            signature = hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest()
            signatures = run.incremental_state["page_signatures"]
            if signature in signatures:
                raise ProviderUnavailable("history_repeated_page")
            signatures.append(signature)
            persist_items(
                run,
                config,
                sorted(items, key=lambda item: (item.timestamp, item.message_id)),
            )
            run.offset += len(result)
            run.status_message = f"Получено новых сообщений: {run.imported_count}. Анализ выполняется в очереди."
        if not result or (beyond and not accepted):
            # Only a completely saved interval advances the durable checkpoint.
            job.new_messages_since, job.last_checked_at = run.cutoff_at, timezone.now()
            job.save(update_fields=["new_messages_since", "last_checked_at"])
            run.state, run.offset, run.incremental_state = "watching", 0, {}
            run.status_message = (
                "Ожидаем новые сообщения. Мониторинг включён постоянно."
            )
            schedule_next(run, job.new_message_poll_seconds)
        else:
            schedule_next(run)
