"""Durable, resumable history imports. Provider access is injected by tasks.py."""

import hashlib
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import quote, urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    HISTORY_ACTIVE_STATES,
    AuditEvent,
    MessageProcessingTrace,
    OutboxEvent,
    RawMessage,
    WhatsAppConfig,
    WhatsAppHistoryItem,
    WhatsAppHistoryJob,
    WhatsAppHistoryRun,
)
from .providers import ProviderUnavailable

IMPORT_STATES = set(HISTORY_ACTIVE_STATES) - {"analyzing", "paused"}
PERMANENT_ERRORS = {
    "history_source_changed",
    "history_source_unavailable",
    "history_invalid_message",
    "history_wrong_chat",
    "history_repeated_page",
    "history_storage_disabled",
    "history_incremental_filter_unsupported",
    "history_waha_http_401",
    "history_waha_http_403",
    "history_waha_http_404",
    "history_waha_http_422",
}
ERROR_LABELS = {
    "history_incremental_filter_unsupported": "WAHA не применяет фильтр новых сообщений. Проверьте версию WAHA перед продолжением.",
    "history_source_changed": "Группа, сессия или команда изменились. Запустите новое задание с текущими настройками.",
    "history_source_unavailable": "Группа выключена или не привязана к активной команде.",
    "history_invalid_message": "WAHA вернул сообщение в неподдерживаемом формате. Импорт не выполнен.",
    "history_wrong_chat": "WAHA вернул данные другого чата. Импорт остановлен.",
    "history_repeated_page": "WAHA повторяет одну страницу истории. Импорт остановлен, чтобы не потерять сообщения.",
    "history_storage_disabled": "В WAHA должно быть включено постоянное хранение истории NOWEB (store.enabled и fullSync).",
    "HTTPError": "WAHA отклонил запрос. Проверьте доступность выбранной группы и настройки WAHA.",
    "ConnectionError": "Не удалось соединиться с WAHA. Можно повторить запуск после восстановления связи.",
    "provider_timeout": "WAHA не ответил вовремя. Можно повторить запуск.",
    "history_waha_http_401": "WAHA отклонил ключ доступа. Проверьте настройки сервера.",
    "history_waha_http_403": "WAHA запретил доступ. Проверьте подключённый аккаунт и группу.",
    "history_waha_http_404": "Сессия или группа не найдена в WAHA. Проверьте настройки источника.",
    "history_waha_http_422": "WAHA не может прочитать историю в текущем состоянии сессии.",
}


def source(config):
    if (
        not config.is_active
        or not config.team_id
        or not config.team.is_active
        or not config.group_jid.strip()
        or not config.session_name.strip()
    ):
        raise ProviderUnavailable("history_source_unavailable")
    return {
        "config_id": config.id,
        "team_id": config.team_id,
        "session_name": config.session_name,
        "chat_id": config.group_jid,
    }


def enqueue_step(run, delay=0):
    run.step += 1
    run.save()
    OutboxEvent.objects.create(
        event_type="history_import",
        deduplication_key=f"history:{run.id}:{run.step}",
        payload={"history_run_id": run.id,
                        "priority": "history", "step": run.step},
        next_attempt_at=timezone.now() + timedelta(seconds=delay),
    )


@transaction.atomic
def start_job(job_id, user=None, *, scheduled=False):
    job = WhatsAppHistoryJob.objects.select_for_update().get(pk=job_id)
    config = (
        WhatsAppConfig.objects.select_for_update(of=("self",))
        .select_related("team")
        .get(pk=job.config_id)
    )
    if not job.enabled:
        raise ValidationError("Сначала включите задание в его настройках.")
    active = job.runs.filter(run_kind="monitor" if job.only_new else "full", state__in=HISTORY_ACTIVE_STATES).first()
    if active:
        if not job.only_new or scheduled:
            raise ValidationError("Для этой группы уже есть незавершённый запуск.")
        if active.state == "paused":
            return control_run(active.pk, "resume", user)
        # Speed up the existing next step; never fork a second monitor.
        OutboxEvent.objects.filter(
            event_type="history_import",
            payload__history_run_id=active.pk,
            state="pending",
        ).update(next_attempt_at=timezone.now())
        job.next_run_at = timezone.now()
        job.save(update_fields=["next_run_at"])
        return active
    job.full_clean()
    snapshot = source(config)
    if job.only_new:
        from .new_messages import initialize_checkpoint

        initialize_checkpoint(job, config)
    run = WhatsAppHistoryRun.objects.create(
        job=job,
        run_kind="monitor" if job.only_new else "full",
        requested_by=user,
        source_snapshot=snapshot,
        settings_snapshot={
            name: getattr(job, name)
            for name in (
                "only_new",
                "new_message_poll_seconds",
                "page_size",
                "initial_wait_seconds",
                "poll_seconds",
                "stable_scans_required",
                "analyze_after_import",
            )
        },
        status_message="Проверяем подключение к WhatsApp.",
    )
    if not job.only_new:
        run.settings_snapshot["analysis_mode"] = "reprocess_all"
        from .models import AISettings
        from .history_analysis import POLICY
        cfg = AISettings.get_active()
        from .processing_attempts import ANALYSIS_CONFIG_FIELDS
        run.settings_snapshot.update(analysis_policy=POLICY, full_history_policy='thread-context-v2', **{
            name: getattr(cfg, name) for name in (
                "analysis_input_token_limit", "analysis_target_message_limit", "analysis_output_token_limit", *ANALYSIS_CONFIG_FIELDS)
        })
    enqueue_step(run)
    job.next_run_at = (
        timezone.now()
        if job.only_new
        else timezone.now() + timedelta(minutes=job.interval_minutes)
        if job.interval_minutes or job.only_new
        else None
    )
    job.save(update_fields=["next_run_at"])
    AuditEvent.objects.create(
        actor=user,
        target_type="WhatsAppHistoryRun",
        target_id=run.id,
        action="history_start",
        before_after={"source": snapshot},
    )
    return run


@transaction.atomic
def control_run(run_id, action, user):
    initial = WhatsAppHistoryRun.objects.get(pk=run_id)
    job = WhatsAppHistoryJob.objects.select_for_update().get(pk=initial.job_id)
    run = WhatsAppHistoryRun.objects.select_for_update().get(pk=run_id)
    if action == "pause" and run.import_kind == "file":
        raise ValidationError("TXT-импорт можно отменить; пауза не поддерживается.")
    if action == "pause" and run.state in IMPORT_STATES:
        run.resume_state, run.state = run.state, "paused"
        run.status_message = "Импорт приостановлен администратором. Прогресс сохранён."
    elif action == "resume" and run.state == "paused":
        if not job.enabled:
            raise ValidationError("Сначала включите задание в настройках.")
        actual = source(
            WhatsAppConfig.objects.select_related("team").get(pk=job.config_id)
        )
        if any(run.source_snapshot.get(k) != v for k, v in actual.items()):
            raise ValidationError(ERROR_LABELS["history_source_changed"])
        run.state = run.resume_state or "waiting_connection"
        run.resume_state = ""
        run.status_message = "Импорт продолжен с сохранённого шага."
    elif action == "cancel" and run.state in IMPORT_STATES | {"paused", "analyzing"}:
        if run.settings_snapshot.get("only_new"):
            job.enabled, job.next_run_at = False, None
            job.save(update_fields=["enabled", "next_run_at"])
        run.state, run.finished_at = "cancelled", timezone.now()
        run.status_message = "Запуск отменён. Сообщения и готовые результаты сохранены. Уже начатый запрос AI может завершиться; новые повторы этого запуска отменены."
        run.items.all().delete()
        from . import ai_retries
        for event in OutboxEvent.objects.select_for_update().filter(
            event_type="extract_message", payload__history_run_id=run.id,
            state__in=["pending", "enqueued", "processing"],
        ).order_by("id"):
            if event.state == "processing":
                # Keep the claim until the running request returns, so a new
                # run cannot analyze the same source concurrently.
                event.payload = {**event.payload, "history_cancelled": True}
                event.save(update_fields=["payload"])
            else:
                event.state, event.lease_until = "cancelled", None
                event.error_code = event.error_code or "history_run_cancelled"
                event.save(update_fields=["state", "lease_until", "error_code"])
                ai_retries.record_attempt(event, "cancelled", "history_run_cancelled")
            from .history_analysis import synchronize
            synchronize(event)
    else:
        raise ValidationError(
            "Действие недоступно на этом этапе. Незавершённый запуск можно отменить; завершённый запуск нельзя продолжить."
        )
    # Invalidate an in-flight page result and queued copies before scheduling a resume.
    OutboxEvent.objects.filter(
        event_type="history_import",
        payload__history_run_id=run.id,
        state__in=["pending", "enqueued", "processing"] if action == "cancel" else ["pending", "enqueued"],
    ).update(state="cancelled")
    run.step += 1
    run.save()
    if run.settings_snapshot.get("only_new"):
        job.next_run_at = timezone.now() if action == "resume" else None
        job.save(update_fields=["next_run_at"])
    if action == "resume":
        enqueue_step(run)
    AuditEvent.objects.create(
        actor=user,
        target_type="WhatsAppHistoryRun",
        target_id=run.id,
        action=f"history_{action}",
    )
    return run


@transaction.atomic
def reanalyze_saved(run_id, user):
    """A new run owns its own results; WAHA history is not fetched again."""
    old = WhatsAppHistoryRun.objects.select_for_update().get(pk=run_id)
    if old.settings_snapshot.get("only_new") or not old.messages.exists():
        raise ValidationError("Для переанализа нужна сохранённая история полного запуска.")
    if old.state in HISTORY_ACTIVE_STATES:
        control_run(old.id, "cancel", user)
    run = start_job(old.job_id, user)
    if any(run.source_snapshot.get(k) != old.source_snapshot.get(k) for k in ("config_id", "team_id", "session_name", "chat_id")):
        raise ValidationError("Источник изменён. Сохранённую историю нельзя анализировать как другой чат.")
    OutboxEvent.objects.filter(event_type="history_import", payload__history_run_id=run.id).update(state="cancelled")
    run.messages.set(old.messages.all())
    run.state, run.cutoff_at = "analyzing", old.cutoff_at
    run.fetched_count = run.existing_count = run.messages.count()
    run.no_text_count = sum(not raw.content.strip() for raw in run.messages.all())
    run.step += 1
    run.status_message = "Переанализ сохранённых сообщений без повторной загрузки WAHA."
    run.settings_snapshot["reanalyzed_from_run_id"] = old.id
    from .history_analysis import schedule
    schedule(run)
    enqueue_step(run, 5)
    run.save()
    AuditEvent.objects.create(actor=user, target_type="WhatsAppHistoryRun", target_id=run.id, action="history_reanalyze_saved", before_after={"previous_run_id":old.id,"messages":run.fetched_count})
    return run


def schedule_due_jobs():
    now = timezone.now()
    ids = (
        WhatsAppHistoryJob.objects.filter(enabled=True)
        .filter(Q(only_new=True) | Q(interval_minutes__gt=0))
        .exclude(runs__state__in=HISTORY_ACTIVE_STATES)
        .filter(Q(next_run_at__lte=now) | Q(next_run_at__isnull=True))
        .values_list("id", flat=True)
    )
    for pk in ids:
        try:
            start_job(pk, scheduled=True)
        except (ValidationError, ProviderUnavailable):
            # An active run owns its progress; it never overlaps a periodic run.
            WhatsAppHistoryJob.objects.filter(pk=pk).update(
                next_run_at=now + timedelta(minutes=1)
            )


def progress(run):
    if run.settings_snapshot.get("analysis_policy"):
        from .history_analysis import counts
        return counts(run)
    total = run.messages.count()
    if run.settings_snapshot.get("analysis_mode") == "reprocess_all":
        events = OutboxEvent.objects.filter(
            event_type="extract_message",
            payload__history_run_id=run.id,
        )
        successful = MessageProcessingTrace.objects.filter(
            operation_key__in=events.values("deduplication_key"), status="success",
        )
        errors = events.filter(state__in=["failed", "unknown", "done", "cancelled"]).exclude(
            deduplication_key__in=successful.values("operation_key"),
        ).count()
        processed = successful.filter(
            raw_message_id__in=run.messages.exclude(content__regex=r"^\s*$").values("id"),
        ).values("raw_message_id").distinct().count() + run.no_text_count
        return {
            "total": total,
            "processed": processed,
            "errors": errors,
            "pending": max(0, total - processed - errors),
        }
    processed = run.messages.filter(processed=True).count()
    errors = run.messages.filter(processing_state="failed").count()
    return {
        "total": total,
        "processed": processed,
        "errors": errors,
        "pending": max(0, total - processed - errors),
    }


def fail_run(payload, code):
    WhatsAppHistoryRun.objects.filter(
        pk=payload.get("history_run_id"),
        step=payload.get("step"),
        state__in=IMPORT_STATES | {"analyzing"},
    ).update(
        state="failed",
        error_code=code,
        status_message=ERROR_LABELS.get(
            code,
            "Не удалось выполнить шаг импорта. Проверьте ошибку очереди и повторите запуск.",
        ),
        finished_at=timezone.now(),
        updated_at=timezone.now(),
    )


def locked_run(run_id, step):
    """Called inside a transaction; lock order also matches admin controls."""
    initial = WhatsAppHistoryRun.objects.get(pk=run_id)
    job = WhatsAppHistoryJob.objects.select_for_update().get(pk=initial.job_id)
    config = (
        WhatsAppConfig.objects.select_for_update(of=("self",))
        .select_related("team")
        .get(pk=job.config_id)
    )
    run = WhatsAppHistoryRun.objects.select_for_update().get(pk=run_id)
    if run.step != step or run.state not in IMPORT_STATES | {"analyzing"}:
        return None, None
    if run.state != "analyzing":
        actual = source(config)
        if any(run.source_snapshot.get(k) != v for k, v in actual.items()):
            raise ProviderUnavailable("history_source_changed")
        if not job.enabled:
            run.resume_state, run.state = run.state, "paused"
            run.status_message = "Задание выключено. Прогресс сохранён; включите настройку и продолжите запуск."
            run.save()
            return None, None
    return run, config


def finish(run, state, message):
    run.state, run.status_message, run.finished_at = state, message, timezone.now()
    run.save()
    if run.import_kind == "file":
        return
    job = run.job
    job.next_run_at = (
        timezone.now() + timedelta(minutes=job.interval_minutes)
        if job.interval_minutes
        else None
    )
    job.save(update_fields=["next_run_at"])


def validate_page(page, run):
    if not isinstance(page, list):
        raise ProviderUnavailable("history_invalid_message")
    items = {}
    cutoff = run.cutoff_at.timestamp()
    for message in page:
        if not isinstance(message, dict):
            raise ProviderUnavailable("history_invalid_message")
        mid, stamp, body = (
            message.get("id"),
            message.get("timestamp"),
            message.get("body") or "",
        )
        if (
            not isinstance(mid, str)
            or not mid
            or len(mid) > 128
            or type(stamp) not in (int, float)
            or not 0 < stamp <= cutoff
            or not isinstance(body, str)
        ):
            raise ProviderUnavailable("history_invalid_message")
        if (
            message.get("from") != run.source_snapshot["chat_id"]
            and message.get("to") != run.source_snapshot["chat_id"]
        ):
            raise ProviderUnavailable("history_wrong_chat")
        try:
            sent = datetime.fromtimestamp(stamp, dt_timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise ProviderUnavailable("history_invalid_message") from None
        items[mid] = WhatsAppHistoryItem(
            run=run,
            message_id=mid,
            timestamp=sent,
            payload=message,
            seen_scan=run.scan_number,
        )
    return list(items.values())


def persist_items(run, config, batch):
    from .whatsapp_identity import ingest_waha_batch
    keys,stored,before=[],{},set()
    originals=[]
    for item in batch:
        msg=item.payload
        from .participants import message_sender
        author=message_sender(msg)
        content=msg.get("body") or ""
        revision=hashlib.sha256(content.encode()).hexdigest()
        identity=(item.message_id,revision)
        originals.append(dict(message_id=item.message_id,content=content,timestamp=item.timestamp,
            sender_phone=author.phone,sender_name=author.name,raw_payload={"event":"history.import","session":config.session_name,"payload":msg}))
        keys.append(identity)
    for identity,(raw,created) in zip(keys,ingest_waha_batch(config,originals),strict=True):
        stored[identity]=raw
        if not created:before.add(identity)
    if originals:
        from .participants import enqueue_participants
        enqueue_participants(config, message_evidence=[{"raw_id": stored[key].id, "payload": item["raw_payload"]["payload"]} for key, item in zip(keys, originals, strict=True) if stored[key].source == "whatsapp_export"])
    outbox, linked = [], []
    counted = (
        set(
            run.messages.filter(pk__in=[m.pk for m in stored.values()]).values_list(
                "pk", flat=True
            )
        )
    )
    queued = set(
        OutboxEvent.objects.filter(
            deduplication_key__in=[f"extract:{m.pk}" for m in stored.values()]
        ).values_list("deduplication_key", flat=True)
    )
    for key in keys:
        raw = stored[key]
        if (
            raw.config_id != config.id
            or raw.chat_id != config.group_jid
            or raw.team_id not in (None, config.team_id)
        ):
            raise ProviderUnavailable("history_wrong_chat")
        from .message_artifacts import register
        register(raw)
        linked.append(raw)
        if raw.pk not in counted:
            run.existing_count += int(key in before)
            run.imported_count += int(key not in before)
            run.no_text_count += int(not bool(raw.content.strip()))
            if run.settings_snapshot.get("only_new"):
                run.fetched_count += 1
        if run.settings_snapshot.get("analysis_policy") or raw.processing_state == "deduplication_ambiguous":
            continue
        if (
            run.settings_snapshot.get("analysis_mode") == "reprocess_all"
            and raw.content.strip()
            and run.settings_snapshot["analyze_after_import"]
        ):
            from .processing_attempts import reserve_attempt

            operation = f"history:{run.id}:extract:{raw.id}"
            locked = RawMessage.objects.select_for_update().get(pk=raw.id)
            trace = reserve_attempt(locked, operation)
            _, created = OutboxEvent.objects.get_or_create(
                deduplication_key=operation,
                defaults={
                    "event_type": "extract_message",
                    "payload": {
                        "raw_id": raw.id,
                        "trace_id": trace.id,
                        "history_run_id": run.id,
                    },
                },
            )
            run.scheduled_count += int(created)
        elif (
            raw.content.strip()
            and not raw.processed
            and run.settings_snapshot["analyze_after_import"]
            and f"extract:{raw.id}" not in queued
        ):
            outbox.append(
                OutboxEvent(
                    event_type="extract_message",
                    deduplication_key=f"extract:{raw.id}",
                    payload={"raw_id": raw.id},
                )
            )
            run.scheduled_count += 1
    run.messages.add(*linked)
    OutboxEvent.objects.bulk_create(outbox, ignore_conflicts=True, batch_size=250)


def import_messages(run, config):
    """The complete staged history and extraction outbox become visible together."""
    current = []

    for item in run.items.order_by("timestamp", "message_id").iterator(chunk_size=250):
        current.append(item)
        if len(current) == 250:
            persist_items(run, config, current)
            current = []
    if current:
        persist_items(run, config, current)
    run.items.all().delete()
    if run.settings_snapshot.get("analysis_policy") and run.settings_snapshot["analyze_after_import"]:
        from .history_analysis import schedule
        schedule(run)
    AuditEvent.objects.create(
        actor=run.requested_by,
        target_type="WhatsAppHistoryRun",
        target_id=run.id,
        action="history_imported",
        before_after={
            "imported": run.imported_count,
            "existing": run.existing_count,
            "scheduled": run.scheduled_count,
        },
    )
    if run.settings_snapshot["analyze_after_import"]:
        run.state = "analyzing"
        run.status_message = "История сохранена. Ожидаем обработку сообщений; факты требуют подтверждения."
        enqueue_step(run, run.settings_snapshot["poll_seconds"])
    else:
        finish(
            run,
            "completed",
            "История сохранена. Автоматический анализ выключен в параметрах этого запуска.",
        )


def process_step(payload, waha):
    if WhatsAppHistoryRun.objects.filter(
        pk=payload["history_run_id"], settings_snapshot__only_new=True
    ).exists():
        from .new_messages import process_new_messages

        return process_new_messages(payload, waha)
    run_id, expected_step = payload["history_run_id"], payload["step"]
    with transaction.atomic():
        run, config = locked_run(run_id, expected_step)
        if run is None:
            return
    settings = run.settings_snapshot
    session = quote(run.source_snapshot["session_name"], safe="")
    result = None
    if run.state == "waiting_connection":
        result = waha("GET", f"/api/sessions/{session}")
        if result.get("status") == "WORKING":
            engine = (result.get("config") or {}).get("noweb", {}).get("store", {})
            if not engine.get("enabled") or not engine.get("fullSync"):
                raise ProviderUnavailable("history_storage_disabled")
            group = waha(
                "GET",
                f"/api/{session}/groups/{quote(run.source_snapshot['chat_id'], safe='')}",
            )
            if group.get("id") != run.source_snapshot["chat_id"]:
                raise ProviderUnavailable("history_wrong_chat")
            result["group_title"] = str(group.get("subject") or "")[:255]
    elif run.state == "collecting":
        params = {
            "limit": settings["page_size"],
            "offset": run.offset,
            "sortBy": "timestamp",
            "sortOrder": "asc",
            "downloadMedia": "false",
            "filter.timestamp.lte": int(run.cutoff_at.timestamp()),
        }
        result = waha(
            "GET",
            f"/api/{session}/chats/{quote(run.source_snapshot['chat_id'], safe='')}/messages?{urlencode(params)}",
            timeout=60,
        )
    with transaction.atomic():
        run, config = locked_run(run_id, expected_step)
        if run is None:
            return
        run.error_code = ""
        if run.state == "waiting_connection":
            status = result.get("status", "UNKNOWN")
            WhatsAppConfig.objects.filter(pk=config.pk).update(status=status)
            if status != "WORKING":
                run.status_message = (
                    f"WhatsApp: {status}. Привяжите сессию в центре управления WAHA."
                )
                enqueue_step(run, settings["poll_seconds"])
            else:
                run.source_snapshot["group_title"] = result["group_title"]
                run.state = "waiting_sync"
                run.stage_ready_at = timezone.now() + timedelta(
                    seconds=settings["initial_wait_seconds"]
                )
                run.status_message = f"Группа «{result['group_title']}» доступна. Ожидаем начальную синхронизацию истории."
                enqueue_step(run, settings["initial_wait_seconds"])
        elif run.state == "waiting_sync":
            if run.stage_ready_at and run.stage_ready_at > timezone.now():
                enqueue_step(
                    run,
                    max(1, int((run.stage_ready_at - timezone.now()).total_seconds())),
                )
            else:
                run.cutoff_at, run.state = (
                    timezone.now().replace(microsecond=0),
                    "collecting",
                )
                run.status_message = "Читаем все доступные страницы истории."
                enqueue_step(run)
        elif run.state == "collecting":
            items = validate_page(result, run)
            if items:
                signature = hashlib.sha256(
                    json.dumps([i.message_id for i in items]).encode()
                ).hexdigest()
                if signature == run.last_page_signature:
                    raise ProviderUnavailable("history_repeated_page")
                run.last_page_signature = signature
                WhatsAppHistoryItem.objects.bulk_create(
                    items,
                    update_conflicts=True,
                    unique_fields=["run", "message_id"],
                    update_fields=["timestamp", "payload", "seen_scan"],
                )
                # Providers may return fewer records than requested without EOF.
                run.offset += len(result)
                run.fetched_count = run.items.filter(seen_scan=run.scan_number).count()
                run.status_message = (
                    f"Проход {run.scan_number}: получено {run.fetched_count} сообщений."
                )
                enqueue_step(run)
            else:
                run.items.exclude(seen_scan=run.scan_number).delete()
                digest = hashlib.sha256()
                for item in run.items.order_by("message_id").iterator(chunk_size=250):
                    digest.update(
                        json.dumps(
                            [
                                item.message_id,
                                item.timestamp.isoformat(),
                                item.payload.get("body") or "",
                            ],
                            ensure_ascii=False,
                        ).encode()
                    )
                signature = digest.hexdigest()
                run.stable_scans = (
                    run.stable_scans + 1 if signature == run.last_digest else 1
                )
                run.last_digest, run.fetched_count = signature, run.items.count()
                if run.stable_scans >= settings["stable_scans_required"]:
                    if not run.fetched_count:
                        finish(
                            run,
                            "empty",
                            "В хранилище WAHA нет доступных сообщений выбранной группы. Сверьте ID группы и синхронизацию истории; это не означает, что история на телефоне пуста.",
                        )
                    else:
                        run.state, run.status_message = (
                            "importing",
                            "История стабилизировалась. Сохраняем сообщения перед анализом.",
                        )
                        enqueue_step(run)
                else:
                    run.scan_number += 1
                    run.offset, run.last_page_signature = 0, ""
                    run.status_message = f"Стабильных проходов: {run.stable_scans} из {settings['stable_scans_required']}."
                    enqueue_step(run, settings["poll_seconds"])
        elif run.state == "importing":
            import_messages(run, config)
        elif run.state == "analyzing":
            counts = progress(run)
            # Failed messages may still have a scheduled provider retry.
            scope = (
                {"payload__history_run_id": run.id}
                if settings.get("analysis_mode") == "reprocess_all"
                else {"payload__raw_id__in": run.messages.values_list("id", flat=True)}
            )
            unfinished = OutboxEvent.objects.filter(
                event_type="extract_message",
                state__in=["pending", "enqueued", "processing"],
                **scope,
            ).exists()
            if counts["pending"] or unfinished:
                from .history_analysis import summary
                run.status_message = summary(counts)
                enqueue_step(run, settings["poll_seconds"])
            else:
                from .history_analysis import summary
                finish(
                    run,
                    "completed_with_errors" if counts["errors"] or counts.get("insufficient_data") else "completed",
                    ("Проход завершён с ошибками. " if counts["errors"] else "Проход завершён; есть сообщения, требующие уточнения. " if counts.get("insufficient_data") else "Проход завершён успешно. ")
                    + summary(counts),
                )
