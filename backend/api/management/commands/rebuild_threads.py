from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from api.thread_backfill import enqueue_backfill


class Command(BaseCommand):
    help = "Queue audited history segmentation. Repeating a request key is idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--config-id", type=int, required=True)
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--request-key", required=True)

    def handle(self, *args, **options):
        if not 1 <= len(options["request_key"]) <= 64:
            raise ValueError("request-key must contain 1–64 characters")
        event = enqueue_backfill(
            User.objects.get(pk=options["user_id"]),
            options["config_id"],
            options["request_key"],
        )
        self.stdout.write(f"Queued outbox event {event.id}: {event.state}")
