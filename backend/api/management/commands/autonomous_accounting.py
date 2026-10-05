import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.autonomous import decide
from api.models import AISettings, FactCandidate, OutboxEvent, RawMessage, SourceWorkItem, WhatsAppConfig


class Command(BaseCommand):
    help = "Preview, enable and recover autonomous WhatsApp accounting. Never prints secrets or source text."

    def add_arguments(self, parser):
        parser.add_argument("--preview", action="store_true")
        parser.add_argument("--enable", action="store_true")
        parser.add_argument("--disable", action="store_true")
        parser.add_argument("--crm", action="store_true")
        parser.add_argument("--backfill", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, **options):
        cfg = AISettings.get_active()
        if not cfg.pk or options["limit"] < 1 or options["limit"] > 5000:
            raise CommandError("Нужна активная AI-конфигурация и limit 1–5000.")
        if options["preview"] and any(options[key] for key in ("enable", "disable", "crm", "backfill")):
            raise CommandError("Preview не может изменять режим или запускать backfill.")
        if options["enable"] and options["disable"]:
            raise CommandError("Выберите enable либо disable.")
        if options["enable"] or options["disable"]:
            cfg.autonomous_enabled = options["enable"]
            if options["disable"]:
                cfg.autonomous_crm_enabled = False
            cfg.save(update_fields=["autonomous_enabled", "autonomous_crm_enabled"])
        if options["crm"]:
            if not cfg.autonomous_enabled:
                raise CommandError("Для CRM сначала нужен автономный режим.")
            cfg.autonomous_crm_enabled = True
            cfg.save(update_fields=["autonomous_crm_enabled"])
        if options["backfill"]:
            if not cfg.autonomous_enabled:
                raise CommandError("Для backfill нужен автономный режим.")
            count = 0
            for config in WhatsAppConfig.objects.filter(is_active=True, team__is_active=True):
                ids = list(RawMessage.objects.filter(config=config, source="waha").exclude(processing_state__in=("deleted", "superseded")).exclude(content="").order_by("timestamp", "id").values_list("id", flat=True)[:options["limit"]])
                size = max(1, cfg.autonomous_context_messages)
                for offset in range(0, len(ids), size):
                    batch = ids[offset:offset + size]
                    with transaction.atomic():
                        SourceWorkItem.objects.bulk_create([SourceWorkItem(raw_message_id=pk, processing_version=cfg.autonomous_policy_version) for pk in batch], ignore_conflicts=True)
                        _, created = OutboxEvent.objects.get_or_create(deduplication_key=f"autonomous-backfill:{config.id}:{cfg.autonomous_policy_version}:{batch[0]}", defaults={"event_type": "extract_message", "payload": {"raw_id": batch[0], "batch_ids": batch, "reanalyze": True, "priority": "history"}})
                        count += int(created)
            self.stdout.write(json.dumps({"scheduled_batches": count, "policy": cfg.autonomous_policy_version}))
        if options["preview"]:
            counts, results = Counter(), []
            for candidate_id in FactCandidate.objects.filter(status="pending", team__is_active=True).order_by("id").values_list("id", flat=True)[:options["limit"]]:
                result = decide(candidate_id, preview=True)
                if result:
                    counts[f"{result['outcome']}:{result['reason_code']}"] += 1
                    results.append({"candidate_id": candidate_id, "outcome": result["outcome"], "reason_code": result["reason_code"]})
            self.stdout.write(json.dumps({"counts": dict(counts), "results": results}, ensure_ascii=False))
