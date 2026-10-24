"""Queue and inspect read-only AI relevance audits; provider calls stay in Q2."""
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from api.batch_screening import audit_report, schedule_audit
from api.models import OutboxEvent


class Command(BaseCommand):
    help = "Queue a durable off-topic audit of every saved message, without extracting or applying facts."

    def add_arguments(self, parser):
        parser.add_argument("--label", help="Unique resumable audit label")
        parser.add_argument("--config-id", type=int)
        parser.add_argument("--report", type=int, help="Read a previously queued audit")
        parser.add_argument("--output", help="Write the full decision list as JSON")

    def handle(self, *args, **options):
        if options["report"]:
            try:
                audit = OutboxEvent.objects.get(pk=options["report"], event_type="screening_audit")
            except OutboxEvent.DoesNotExist as exc:
                raise CommandError("Audit not found") from exc
        elif options["label"]:
            audit = schedule_audit(options["label"], options["config_id"])
        else:
            raise CommandError("Specify --label to queue an audit or --report ID to inspect it")
        value = audit_report(audit)
        if options["output"]:
            Path(options["output"]).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        self.stdout.write(json.dumps({key: value for key, value in value.items() if key != "decisions"}, ensure_ascii=False, default=str))
