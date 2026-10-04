"""Permission-checked, resumable history reclassification through the same pipeline."""

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from . import access
from .models import RawMessage, OutboxEvent
from .processing_attempts import require_reanalysis, reserve_attempt


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
