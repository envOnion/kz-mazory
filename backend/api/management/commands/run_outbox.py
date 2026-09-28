import signal
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from api.tasks import dispatch_outbox, monitor_kpi_risks_and_anomalies_task


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
        while running:
            close_old_connections()
            dispatch_outbox()
            if time.monotonic() - planned >= 60:
                monitor_kpi_risks_and_anomalies_task()
                planned = time.monotonic()
            if options["once"]:
                return
            time.sleep(1)
