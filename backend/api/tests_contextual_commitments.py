from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from . import access
from .commitment_refresh import schedule_commitment_refresh
from .commitment_resolution import resolve_remaining
from .facts import json_value, review
from .message_time import source_time
from .models import (
    Team,
    TeamMembership,
    WhatsAppConfig,
    RawMessage,
    MessageProcessingTrace,
    FactCandidate,
    FactEvidence,
    Commitment,
    AISettings,
    OutboxEvent,
)
from .pipeline import _facts
from .providers import ProviderUnavailable

ZONE = dt_timezone(timedelta(hours=6))


class ContextualCommitmentTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Команда")
        self.config = WhatsAppConfig.objects.create(
            team=self.team,
            group_jid="team@g.us",
            snapshot={"timezone": "UTC+06:00", "message_format": "whatsapp_export"},
        )
        self.user = User.objects.create_user("lead")
        TeamMembership.objects.create(
            user=self.user, team=self.team, role="team_lead", status="active"
        )
        self.request = self.message(
            "[19.08.2026, 18:41] Марат:\nПришлите список объектов за 2026 год из таблицы https://example.test/list"
        )
        self.promise = self.message(
            "[19.08.2026, 23:58] Жанат:\nЗавтра с утра отправлю список"
        )
        self.done = self.message(
            "[20.08.2026, 08:45] Жанат:\nОтправил список объектов за 2026 год из таблицы"
        )

    def message(self, content, config=None):
        return RawMessage.objects.create(
            config=config or self.config,
            team=(config or self.config).team,
            chat_id=(config or self.config).group_jid,
            message_id=f"m:{RawMessage.objects.count()}",
            timestamp=timezone.now(),
            received_at=timezone.now(),
            sent_at_known=True,
            sender_name="Импортёр",
            content=content,
        )

    def fact(self, fulfilled=False):
        refs = [
            {
                "raw_message_id": self.request.id,
                "quote": "список объектов за 2026 год",
                "role": "request",
            },
            {
                "raw_message_id": self.promise.id,
                "quote": "Завтра с утра отправлю список",
                "role": "promise",
            },
        ]
        if fulfilled:
            refs.append(
                {
                    "raw_message_id": self.done.id,
                    "quote": "Отправил список объектов за 2026 год из таблицы",
                    "role": "fulfillment",
                }
            )
        return {
            "fact_type": "commitment",
            "object_name": "",
            "commitment_text": "Отправить список объектов за 2026 год из таблицы https://example.test/list",
            "evidence": "Завтра с утра отправлю список",
            "evidence_messages": refs,
            "deadline_at": "2026-10-03T18:00:00+03:00",
            "deadline_precision": "datetime",
            "deadline_message_id": self.promise.id,
            "responsible_name": "Импортёр",
            "commitment_status": "fulfilled" if fulfilled else "pending",
            "fulfillment_message_id": self.done.id if fulfilled else None,
        }

    def validated(self, fulfilled=False):
        return _facts(
            {"facts": [self.fact(fulfilled)]}, self.promise, snapshot_id=self.done.id
        )[0]

    def candidate(self, fact):
        trace = MessageProcessingTrace.objects.create(
            raw_message=self.promise,
            attempt_no=self.promise.traces.count() + 1,
            context_metadata={"snapshot_max_id": self.done.id},
        )
        candidate = FactCandidate.objects.create(
            trace=trace,
            team=self.team,
            fact_type="commitment",
            source_key=f"candidate:{trace.id}",
            proposed_changes=json_value(fact),
        )
        for ref in fact["evidence_messages"]:
            FactEvidence.objects.create(
                candidate=candidate,
                raw_message_id=ref["raw_message_id"],
                quote=ref["quote"],
                field_name=ref["role"],
            )
        return candidate

    def test_tomorrow_uses_original_send_date_and_fixed_offset(self):
        fact = self.validated()
        self.assertEqual(fact["deadline_at"], datetime(2026, 8, 20, 9, tzinfo=ZONE))
        self.assertEqual(fact["responsible_name"], "Жанат")
        self.assertEqual(fact["deadline_basis"], "morning_default")
        self.assertEqual(source_time(self.promise)[2], "export_header")

    def test_midnight_boundary_and_each_replica_has_its_own_date(self):
        next_day = self.message(
            "[20.08.2026, 00:05] Жанат:\nЗавтра с утра отправлю список"
        )
        fact = self.fact()
        fact["deadline_message_id"] = next_day.id
        fact["evidence_messages"].append(
            {
                "raw_message_id": next_day.id,
                "quote": "Завтра с утра отправлю список",
                "role": "deadline",
            }
        )
        result = _facts({"facts": [fact]}, self.promise, snapshot_id=next_day.id)[0]
        self.assertEqual(result["deadline_at"], datetime(2026, 8, 21, 9, tzinfo=ZONE))

    def test_import_date_is_not_send_date_when_header_is_missing(self):
        self.promise.content = "Завтра с утра отправлю список"
        self.promise.save()
        result = self.validated()
        self.assertIsNone(result["deadline_at"])
        self.assertEqual(result["deadline_precision"], "unknown")

    def test_live_messages_after_import_cutoff_keep_transport_dates(self):
        self.config.snapshot["export_imported_until"] = "2026-09-26T00:00:00Z"
        self.config.save()
        live = self.message("Завтра с утра отправлю новый список")
        sent, sender, basis = source_time(live)
        self.assertEqual(basis, "message_metadata")
        self.assertEqual(sent, live.timestamp.astimezone(ZONE))

    def test_native_history_before_export_cutoff_keeps_proven_transport_date(self):
        self.promise.content = 'Поступило 42 млн тенге'
        self.promise.source = 'waha'
        self.promise.raw_payload = {'event': 'history.import', 'session': self.promise.session_name, 'payload': {'id': self.promise.message_id, 'body': self.promise.content, 'timestamp': int(self.promise.timestamp.timestamp())}}
        self.promise.save()
        sent, sender, basis = source_time(self.promise)
        self.assertEqual(basis, 'message_metadata')
        self.assertEqual(sent, self.promise.timestamp.astimezone(ZONE))
        self.promise.raw_payload['payload']['timestamp'] -= 86400
        self.promise.save()
        self.assertEqual(source_time(self.promise)[2], 'unknown_export_date')

    def test_normal_message_metadata_is_used_without_parsing_arbitrary_body(self):
        self.config.snapshot = {"timezone": "UTC+06:00"}
        self.config.save()
        self.promise.config = self.config
        self.promise.timestamp = datetime(2026, 8, 19, 18, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(source_time(self.promise)[0].date().isoformat(), "2026-08-20")
        self.assertEqual(source_time(self.promise)[1], "Импортёр")

    def test_fulfillment_uses_later_quote_and_original_timestamp(self):
        result = self.validated(True)
        self.assertEqual(
            result["fulfilled_at"], datetime(2026, 8, 20, 8, 45, tzinfo=ZONE)
        )
        candidate = self.candidate(result)
        review(candidate.id, self.user, "approve", base_version=0)
        commitment = Commitment.objects.get(candidate=candidate)
        self.assertEqual(commitment.status, "fulfilled")
        self.assertIsNone(commitment.project_id)
        self.assertEqual(commitment.team_id, self.team.id)
        self.assertEqual(commitment.responsible_name, "Жанат")
        self.assertTrue(
            access.commitments_for(self.user).filter(pk=commitment.pk).exists()
        )

    def test_general_acknowledgement_and_link_are_not_fulfillment(self):
        for text in ("Принято", "Ок", "https://example.test/list"):
            self.done.content = f"[20.08.2026, 08:45] Жанат:\n{text}"
            self.done.save()
            fact = self.fact(True)
            fact["evidence_messages"][-1]["quote"] = text
            with self.assertRaisesMessage(
                ProviderUnavailable, "commitment_fulfillment_ambiguous"
            ):
                _facts({"facts": [fact]}, self.promise, snapshot_id=self.done.id)

    def test_agreement_itself_does_not_create_a_task(self):
        agreement = self.message("[19.08.2026, 23:59] Марат:\nТогда завтра")
        fact = self.fact()
        fact.update(
            evidence="Тогда завтра",
            evidence_messages=[
                {
                    "raw_message_id": agreement.id,
                    "quote": "Тогда завтра",
                    "role": "promise",
                }
            ],
        )
        self.assertEqual(
            _facts({"facts": [fact]}, agreement, snapshot_id=agreement.id), []
        )

    def test_foreign_chat_evidence_is_rejected(self):
        team = Team.objects.create(name="Чужая команда")
        config = WhatsAppConfig.objects.create(team=team, group_jid="private@g.us")
        other = self.message("Чужой факт", config)
        fact = self.fact()
        fact["evidence_messages"].append(
            {"raw_message_id": other.id, "quote": "Чужой факт", "role": "request"}
        )
        with self.assertRaisesMessage(
            ProviderUnavailable, "commitment_evidence_unavailable"
        ):
            _facts({"facts": [fact]}, self.promise, snapshot_id=other.id)

    def test_repeat_confirmation_of_same_promise_is_ignored(self):
        fact = self.fact()
        fact["promise_message_id"] = self.request.id
        self.assertEqual(
            _facts({"facts": [fact]}, self.promise, snapshot_id=self.done.id), []
        )

    def test_fulfillment_update_requires_review_and_preserves_original_task(self):
        original = self.candidate(self.validated())
        review(original.id, self.user, "approve", base_version=0)
        task = Commitment.objects.get(candidate=original)
        update = self.validated(True)
        update.update(commitment_id=task.id, base_commitment_version=task.version)
        candidate = self.candidate(update)
        self.assertEqual(Commitment.objects.get(pk=task.pk).status, "pending")
        review(candidate.id, self.user, "approve", base_version=0)
        task.refresh_from_db()
        self.assertEqual(task.status, "fulfilled")
        self.assertEqual(Commitment.objects.count(), 1)
        self.assertEqual(
            task.commitment_text, original.proposed_changes["commitment_text"]
        )
        review(candidate.id, self.user, "approve", base_version=0)
        self.assertEqual(Commitment.objects.count(), 1)

    def test_refresh_is_asynchronous_coalesced_and_scoped(self):
        self.candidate(self.validated())
        self.assertEqual(schedule_commitment_refresh(self.done), 1)
        self.assertEqual(schedule_commitment_refresh(self.done), 0)
        event = OutboxEvent.objects.get(event_type="extract_message")
        self.assertTrue(event.payload["commitment_refresh"])
        self.assertEqual(event.payload["raw_id"], self.promise.id)
        self.assertEqual(Commitment.objects.count(), 0)

    def test_human_deadline_correction_is_not_silently_overwritten(self):
        candidate = self.candidate(self.validated())
        review(
            candidate.id,
            self.user,
            "approve",
            reason="Срок уточнён по договорённости",
            changes={
                "deadline_at": "2026-08-21T10:00:00+06:00",
                "deadline_precision": "datetime",
            },
            base_version=0,
        )
        task = Commitment.objects.get(candidate=candidate)
        self.assertEqual(task.deadline_at, datetime(2026, 8, 21, 10, tzinfo=ZONE))

    def test_team_lead_cannot_read_another_teams_general_task(self):
        other = Team.objects.create(name="Other")
        hidden = Commitment.objects.create(
            team=other, commitment_text="Чужая задача", is_verified=True
        )
        self.assertFalse(
            access.commitments_for(self.user).filter(pk=hidden.pk).exists()
        )

    def test_unassigned_general_task_cannot_be_completed_by_finance_only(self):
        from rest_framework.exceptions import PermissionDenied
        from .notifications import change_commitment

        finance = User.objects.create_user("finance")
        TeamMembership.objects.create(
            user=finance, team=self.team, role="finance", status="active"
        )
        task = Commitment.objects.create(
            team=self.team, commitment_text="Task", is_verified=True
        )
        self.assertTrue(access.commitments_for(finance).filter(pk=task.id).exists())
        with self.assertRaises(PermissionDenied):
            change_commitment(finance, task.id, task.version, "fulfill")

    def test_partial_context_scans_all_remaining_messages_in_bounded_batches(self):
        later = [
            self.message(
                f"[21.08.2026, 10:00] Марат:\nДругой вопрос {i} " + "текст " * 1000
            )
            for i in range(4)
        ]
        trace = MessageProcessingTrace.objects.create(
            raw_message=self.promise,
            earlier_messages_context=[{"raw_message_id": self.request.id}],
            context_metadata={
                "snapshot_max_id": later[-1].id,
                "history_complete_in_request": False,
                "analysis_time": timezone.now().isoformat(),
                "effective_provider_url": "https://provider.test/v1",
                "api_format": "openai_compatible",
            },
        )

        class Counter:
            def count_payload(self, value):
                import json

                return len(json.dumps(value, ensure_ascii=False)) // 4

        cfg = AISettings(
            chat_model_name="test",
            context_window_tokens=4096,
            max_completion_tokens=512,
            context_safety_tokens=128,
        )
        endpoint = {"tag": "test", "api_format": "openai_compatible"}
        with (
            patch(
                "api.commitment_resolution.context_runtime",
                return_value=(Counter(), endpoint),
            ),
            patch(
                "api.commitment_resolution.AIService.analyze_payload",
                return_value=({"facts": [json_value(self.validated())]}, {}, {}),
            ),
        ):
            resolve_remaining([self.validated()], self.promise, cfg, trace)
        batches = trace.context_metadata["commitment_resolution_batches"]
        scanned = {mid for batch in batches for mid in batch["message_ids"]}
        self.assertEqual(scanned, {self.done.id, *(item.id for item in later)})
        self.assertGreater(len(batches), 1)
        self.assertTrue(
            all(Counter().count_payload(batch["request"]) <= 3456 for batch in batches)
        )
