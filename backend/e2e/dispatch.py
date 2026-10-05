"""Local publisher using the genuine Django Q2 ORM broker and a separate worker."""

import time
import json
from django.conf import settings
from django.db import close_old_connections
from api.models import AISettings, OutboxEvent
from django_q.tasks import async_task
from django.utils import timezone


def dispatch_forever():
    while True:
        close_old_connections()
        control = json.loads((settings.E2E_DIR / "provider-state.json").read_text())
        if control.get("autonomous"):
            AISettings.objects.update(autonomous_enabled=True, autonomous_crm_enabled=True)
            OutboxEvent.objects.get_or_create(deduplication_key="e2e-autonomous-reports", defaults={"event_type": "autonomous_reconcile", "payload": {}})
        # Indexing is outside this scenario (no vector database in portable e2e).
        OutboxEvent.objects.filter(event_type="index_message", state="pending").update(
            state="done"
        )
        for event in (
            OutboxEvent.objects.filter(
                state="pending", next_attempt_at__lte=timezone.now()
            )
            .exclude(event_type="index_message")
            .order_by("id")[:100]
        ):
            if OutboxEvent.objects.filter(pk=event.id, state="pending").update(
                state="enqueued"
            ):
                async_task("api.tasks.run_outbox", event.id)
        time.sleep(0.2)
