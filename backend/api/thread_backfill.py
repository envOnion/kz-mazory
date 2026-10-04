"""Permission-checked, resumable history reclassification through the same pipeline."""

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from . import access
from .models import RawMessage, OutboxEvent, WhatsAppHistoryJob, WhatsAppHistoryRun, AuditEvent
from .processing_attempts import require_reanalysis, reserve_attempt


@transaction.atomic
def track_backfill(user, job_id, request_key):
    """Attach existing attempts to a visible history monitor without issuing AI calls."""
    from .history_jobs import source, enqueue_step
    from .models import HISTORY_ACTIVE_STATES
    from django.core.exceptions import ValidationError

    job = WhatsAppHistoryJob.objects.select_for_update().get(pk=job_id)
    config = access.configs_for(user).select_related("team").get(pk=job.config_id)
    access.require_team_role(user, config.team_id, ["team_lead"])
    snapshot = source(config)
    existing = job.runs.filter(
        settings_snapshot__thread_rebuild_request_key=request_key,
        settings_snapshot__thread_rebuild_user_id=user.id,
    ).first()
    if existing:
        return existing
    if job.runs.filter(state__in=HISTORY_ACTIVE_STATES).exists():
        raise ValidationError("Сначала завершите предыдущий запуск истории.")
    messages = access.messages_for(user).filter(
        config=config, source="waha", session_name=config.session_name, chat_id=config.group_jid,
    ).exclude(processing_state__in=["deleted", "superseded"])
    ids = set(messages.values_list("id", flat=True))
    text_ids = set(messages.exclude(content__regex=r"^\s*$").values_list("id", flat=True))
    prefix = f"thread-rebuild:{user.id}:{request_key}:"
    events = [event for event in OutboxEvent.objects.select_for_update().filter(
        event_type="extract_message", deduplication_key__startswith=prefix,
    ) if event.payload.get("raw_id") in text_ids]
    if not text_ids or {event.payload["raw_id"] for event in events} != text_ids:
        raise ValidationError("Тематический разбор ещё не создал задания для всей истории этого источника.")
    if any(event.payload.get("history_run_id") is not None for event in events):
        raise ValidationError("Задания уже принадлежат другому запуску истории.")
    run = WhatsAppHistoryRun.objects.create(
        job=job, requested_by=user, state="analyzing", source_snapshot=snapshot,
        settings_snapshot={"only_new": False, "analysis_mode": "reprocess_all",
            "analyze_after_import": True, "poll_seconds": job.poll_seconds,
            "thread_rebuild_request_key": request_key, "thread_rebuild_user_id": user.id},
        fetched_count=len(ids), existing_count=len(ids), no_text_count=len(ids - text_ids),
        scheduled_count=len(events), status_message="Отслеживаем уже запущенный тематический разбор истории.",
    )
    run.messages.add(*ids)
    for event in events:
        event.payload = {**event.payload, "history_run_id": run.id}
    OutboxEvent.objects.bulk_update(events, ["payload"])
    enqueue_step(run)
    AuditEvent.objects.create(actor=user, target_type="WhatsAppHistoryRun", target_id=run.id,
        action="thread_backfill_tracking", before_after={"request_key": request_key, "events": [e.id for e in events]})
    return run


def enqueue_backfill(user, config_id, request_key):
    config = access.configs_for(user).get(pk=config_id)
    access.require_team_role(user, config.team_id, ["team_lead"])
    return OutboxEvent.objects.get_or_create(
        deduplication_key=f"thread-backfill:{config.id}:{user.id}:{request_key}:0",
        defaults={
            "event_type": "thread_backfill",
            "payload": {
                "config_id": config.id,
                "requested_by_id": user.id,
                "request_key": request_key,
                "cursor": 0,
            },
        },
    )[0]


def backfill_page(payload):
    user = User.objects.get(pk=payload["requested_by_id"])
    config = access.configs_for(user).get(pk=payload["config_id"])
    access.require_team_role(user, config.team_id, ["team_lead"])
    ids = list(
        access.messages_for(user)
        .filter(config=config, id__gt=payload["cursor"])
        .exclude(processing_state__in=["deleted", "superseded"])
        .order_by("id")
        .values_list("id", flat=True)[:100]
    )
    if not ids:
        return
    # Do not compete with attempts already in flight. Retry this page after those finish.
    if OutboxEvent.objects.filter(
        event_type="extract_message",
        payload__raw_id__in=ids,
        state__in=["pending", "enqueued", "processing"],
    ).exists():
        from .providers import ProviderUnavailable

        raise ProviderUnavailable("thread_history_busy", retry_after=10)
    with transaction.atomic():
        for raw in (
            RawMessage.objects.select_for_update(of=("self",))
            .filter(pk__in=ids)
            .select_related("config__team", "team", "project")
            .order_by("id")
        ):
            require_reanalysis(user, raw)
            key = f"thread-rebuild:{user.id}:{payload['request_key']}:{raw.id}"
            trace = reserve_attempt(raw, key)
            OutboxEvent.objects.get_or_create(
                deduplication_key=key,
                defaults={
                    "event_type": "extract_message",
                    "payload": {
                        "raw_id": raw.id,
                        "trace_id": trace.id,
                        "requested_by_id": user.id,
                    },
                },
            )
        cursor = ids[-1]
        OutboxEvent.objects.get_or_create(
            deduplication_key=f"thread-backfill:{config.id}:{user.id}:{payload['request_key']}:{cursor}",
            defaults={
                "event_type": "thread_backfill",
                "next_attempt_at": timezone.now(),
                "payload": {**payload, "cursor": cursor},
            },
        )
