import signal
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from api.tasks import dispatch_outbox, monitor_kpi_risks_and_anomalies_task
from api.history_jobs import schedule_due_jobs
from api.participants import schedule_participant_sync


class Command(BaseCommand):
    help = "Publish durable outbox events and plan reminders. No provider calls in this process."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        running = True

        def stop(*_):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        planned = 0
        history_planned = 0
        participants_planned = 0
        while running:
            close_old_connections()
            if time.monotonic() - history_planned >= 5:
                schedule_due_jobs()
                history_planned = time.monotonic()
            if time.monotonic() - participants_planned >= 60:
                schedule_participant_sync()
                participants_planned = time.monotonic()
            dispatch_outbox()
            if time.monotonic() - planned >= 60:
                from api.models import AISettings, OutboxEvent
                if AISettings.get_active().autonomous_enabled:
                    from django.utils import timezone
                    minute = timezone.now().strftime("%Y%m%d%H%M")
                    OutboxEvent.objects.get_or_create(deduplication_key=f"autonomous-reconcile:{minute}", defaults={"event_type": "autonomous_reconcile", "payload": {}})
                monitor_kpi_risks_and_anomalies_task()
                planned = time.monotonic()
            if options["once"]:
                return
            time.sleep(1)
