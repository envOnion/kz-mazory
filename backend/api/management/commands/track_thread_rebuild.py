from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from api.thread_backfill import track_backfill


class Command(BaseCommand):
    help = "Show an existing thematic rebuild in history admin without duplicating AI jobs."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--request-key", required=True)

    def handle(self, *args, **options):
        run = track_backfill(User.objects.get(pk=options["user_id"]), options["job_id"], options["request_key"])
        self.stdout.write(f"Tracking history run {run.id}: {run.state}")
