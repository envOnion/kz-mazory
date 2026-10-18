"""Idempotent reconstruction of the communication journal from stored data.

Rebuilds CommunicationRecord/CommunicationEvent rows for history created before
the journal existed.  Reuses the exact key derivations of the live signals, so
running it twice or after live traffic converges on the same rows.  Performs
no external calls: no WhatsApp sends, no LLM requests, no CRM writes.
Unknown times are recorded as estimated; unverifiable links stay unlinked.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.communication import (
    journal_notification,
    journal_otp,
    journal_raw,
    on_clarification_saved,
    on_crm_saved,
    on_delivery_saved,
    on_reply_saved,
    on_trace_saved,
)
from api.models import (
    AuditEvent,
    ClarificationReply,
    CommunicationEvent,
    CommunicationRecord,
    CrmDelivery,
    FactClarification,
    MessageProcessingTrace,
    Notification,
    NotificationDelivery,
    OutboxEvent,
    RawMessage,
)

CHECKPOINT_TARGET = "communication_reconstruction"

SOURCES = [
    ("raw", RawMessage, lambda obj: journal_raw(obj)),
    ("notification", Notification, lambda obj: journal_notification(obj)),
    ("otp", OutboxEvent, lambda obj: journal_otp(obj)),
    ("delivery", NotificationDelivery, lambda obj: on_delivery_saved(NotificationDelivery, obj, created=True)),
    ("clarification", FactClarification, lambda obj: on_clarification_saved(FactClarification, obj, created=True)),
    ("reply", ClarificationReply, lambda obj: on_reply_saved(ClarificationReply, obj, created=True)),
    ("trace", MessageProcessingTrace, lambda obj: on_trace_saved(MessageProcessingTrace, obj, created=True)),
    ("crm", CrmDelivery, lambda obj: on_crm_saved(CrmDelivery, obj, created=True)),
]


class Command(BaseCommand):
    help = "Реконструкция журнала коммуникаций из сохранённых записей. Не выполняет внешних отправок."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=500)
        parser.add_argument("--source", choices=[name for name, _, _ in SOURCES], help="Только один источник")
        parser.add_argument("--from-scratch", action="store_true", help="Игнорировать сохранённые контрольные точки")
        parser.add_argument("--report", action="store_true", help="Только отчёт полноты")

    def _checkpoints(self):
        points = {}
        for row in AuditEvent.objects.filter(target_type=CHECKPOINT_TARGET, action="checkpoint").order_by("id"):
            points[row.before_after.get("source")] = row.before_after.get("last_pk", 0)
        return points

    def _save_checkpoint(self, source, last_pk, processed):
        AuditEvent.objects.create(
            target_type=CHECKPOINT_TARGET,
            target_id=last_pk,
            action="checkpoint",
            before_after={"source": source, "last_pk": last_pk, "processed": processed},
        )

    def handle(self, *args, **options):
        if options["report"]:
            self._report()
            return
        batch = max(1, options["batch"])
        points = {} if options["from_scratch"] else self._checkpoints()
        wanted = {options["source"]} if options["source"] else None
        for name, model, replay in SOURCES:
            if wanted and name not in wanted:
                continue
            cursor = points.get(name, 0)
            source_rows = model.objects.filter(event_type="otp") if name == "otp" else model.objects.all()
            total = 0
            while True:
                ids = list(source_rows.filter(pk__gt=cursor).order_by("pk").values_list("pk", flat=True)[:batch])
                if not ids:
                    break
                # One replay per row inside its own small transaction: a bad row
                # can never poison the whole batch; the row keeps its checkpoint.
                for pk in ids:
                    obj = model.objects.filter(pk=pk).first()
                    if obj is not None:
                        with transaction.atomic():
                            replay(obj)
                cursor = ids[-1]
                total += len(ids)
                self._save_checkpoint(name, cursor, total)
                self.stdout.write(f"{name}: обработано {total}, курсор {cursor}")
                if len(ids) < batch:
                    break
            self.stdout.write(self.style.SUCCESS(f"{name}: готово, всего {total}"))
        self._report()

    def _report(self):
        self.stdout.write("Полнота журнала:")
        raw_missing = RawMessage.objects.filter(communication_record__isnull=True).count()
        notif_missing = Notification.objects.filter(communication_record__isnull=True).count()
        otp_missing = OutboxEvent.objects.filter(event_type="otp", communication_record__isnull=True).count()
        self.stdout.write(f"  записи сообщений: {CommunicationRecord.objects.count()} "
                          f"(без записи RawMessage: {raw_missing}, Notification: {notif_missing}, OTP: {otp_missing})")
        self.stdout.write(f"  события: {CommunicationEvent.objects.count()}, "
                          f"событий без свидетельства времени (оценка): "
                          f"{CommunicationEvent.objects.filter(time_is_estimated=True).count()}")
        self.stdout.write("  Внешние вызовы не выполнялись; повторный запуск не создаёт дублей.")
