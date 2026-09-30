from django.core.management.base import BaseCommand, CommandError

from api.models import FactCandidate
from api.tasks import enqueue_crm_match


class Command(BaseCommand):
    help = (
        "Queue read-only CRM matching for pending project candidates. "
        "The command itself never contacts Bitrix24."
    )

    def add_arguments(self, parser):
        parser.add_argument("--candidate-id", action="append", type=int, default=[])
        parser.add_argument("--trace-id", type=int)
        parser.add_argument("--retry-disabled", action="store_true")
        parser.add_argument("--retry-errors", action="store_true")

    def handle(self, *args, **options):
        candidate_ids = options["candidate_id"]
        if any(candidate_id <= 0 for candidate_id in candidate_ids):
            raise CommandError("candidate-id must be a positive integer")
        if options["trace_id"] is not None and options["trace_id"] <= 0:
            raise CommandError("trace-id must be a positive integer")

        allowed_states = ["not_requested"]
        if options["retry_disabled"]:
            allowed_states.append("disabled")
        if options["retry_errors"]:
            allowed_states.append("error")
        queryset = FactCandidate.objects.filter(
            status="pending",
            fact_type="project",
            crm_match_state__in=allowed_states,
        )
        if candidate_ids:
            queryset = queryset.filter(pk__in=candidate_ids)
        if options["trace_id"] is not None:
            queryset = queryset.filter(trace_id=options["trace_id"])

        queued = 0
        for candidate_id in queryset.order_by("id").values_list("id", flat=True):
            if enqueue_crm_match(candidate_id, allowed_states=allowed_states):
                queued += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {queued} pending project candidate(s) for CRM matching."
            )
        )
