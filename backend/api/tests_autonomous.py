from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .autonomous import decide, refresh_checkpoint
from .autonomous_crm import deliver, enqueue_project
from .autonomous_reports import financial_summary, refresh_reports
from .facts import review
from .security import Conflict
from .models import (
    AISettings, BitrixSettings, Commitment, Company, CrmDelivery, FactCandidate,
    FactDecision, FactEvent, FactEvidence, FinancialRecord, MessageArtifact, MessageProcessingTrace,
    OutboxEvent, Project, RawMessage, SourceCheckpoint, Team, TeamMembership,
    WhatsAppConfig,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AutonomousAccountingTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Sales")
        self.config = WhatsAppConfig.objects.create(team=self.team, group_jid="sales@g.us", name="Sales", snapshot={"timezone": settings.TIME_ZONE})
        self.cfg = AISettings.objects.create(name="AI", autonomous_enabled=True)
        self.project = Project.objects.create(team=self.team, name="Объект 343", normalized_name="объект 343", identity_confirmed=True)
        self.counter = 0

    def candidate(self, text="По объекту 343 поступило 117 млн тенге", kind="payment", **changes):
        self.counter += 1
        raw = RawMessage.objects.create(config=self.config, team=self.team, source="waha", message_id=f"m:{self.counter}", timestamp=timezone.now(), chat_id=self.config.group_jid, sender_name="Менеджер", content=text)
        trace = MessageProcessingTrace.objects.create(raw_message=raw, context_metadata={"snapshot_max_id": raw.id, "history_complete_in_request": True})
        data = {"fact_type": kind, "object_name": self.project.name, "evidence": text, "evidence_message_id": raw.id}
        if kind == "payment":
            data.update(amount="117000000", currency="KZT", payment_date=timezone.localdate().isoformat(), payment_kind="increment")
        data.update(changes)
        candidate = FactCandidate.objects.create(trace=trace, team=self.team, project=self.project, base_project_version=self.project.version, fact_type=kind, source_key=f"candidate:{self.counter}", proposed_changes=data)
        FactEvidence.objects.create(candidate=candidate, raw_message=raw, quote=text, field_name="source")
        return candidate

    def webhook(self, payload):
        encoded = json.dumps({'event': 'message.any', 'session': 'default', 'payload': {'timestamp': int(timezone.now().timestamp()), **payload}})
        signature = hmac.new(b'fixture-secret', encoded.encode(), hashlib.sha512).hexdigest()
        with override_settings(WAHA_WEBHOOK_SECRET='fixture-secret'):
            return APIClient().post('/api/whatsapp/webhook/', encoded, content_type='application/json', HTTP_X_WEBHOOK_HMAC=signature)

    def test_captionless_media_is_saved_with_explicit_gap(self):
        response = self.webhook({'id': 'media', 'from': self.config.group_jid, 'body': '', 'hasMedia': True, 'media': None})
        self.assertEqual(response.status_code, 202)
        raw = RawMessage.objects.get(message_id='media')
        self.assertEqual(MessageArtifact.objects.get(raw_message=raw).error_code, 'waha_media_not_downloaded')
        refresh_checkpoint(raw)
        self.assertEqual(SourceCheckpoint.objects.get().counts['missing_media'], 1)

    def test_human_outgoing_message_uses_target_group_and_real_sender(self):
        response = self.webhook({'id': 'own', 'from': '79990000001@c.us', 'to': self.config.group_jid, 'fromMe': True, 'body': 'Завтра предоставлю цену'})
        self.assertEqual(response.status_code, 202)
        raw = RawMessage.objects.get(message_id='own')
        self.assertEqual(raw.chat_id, self.config.group_jid)
        self.assertEqual(raw.sender_phone, '79990000001')
        self.assertEqual(self.webhook({'id': 'foreign', 'from': '79990000001@c.us', 'to': 'other@g.us', 'fromMe': True, 'body': 'Поступило 10 млн'}).status_code, 403)

    def test_system_notification_is_not_accepted_as_business_truth(self):
        from .models import Notification, NotificationDelivery
        user = User.objects.create_user('recipient')
        notification = Notification.objects.create(recipient=user, deduplication_key='synthetic', title='Уведомление', message='Сообщение')
        NotificationDelivery.objects.create(notification=notification, provider_message_id='system')
        response = self.webhook({'id': 'system', 'from': '79990000001@c.us', 'to': self.config.group_jid, 'fromMe': True, 'body': 'Поступило 117 млн'})
        self.assertEqual(response.data['status'], 'ignored')
        self.assertFalse(RawMessage.objects.exists())
        candidate = self.candidate()
        NotificationDelivery.objects.update(provider_message_id=candidate.trace.raw_message.message_id)
        self.assertEqual(decide(candidate.id).reason_code, 'system_generated_message')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_payment_is_accepted_without_human_and_unknown_contract_stays_unknown(self):
        candidate = self.candidate()
        result = decide(candidate.id)
        self.assertEqual(result.outcome, "accepted")
        candidate.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(candidate.status, "approved")
        self.assertIsNone(candidate.reviewed_by_id)
        self.assertEqual(self.project.paid_amount, Decimal("117000000"))
        self.assertFalse(self.project.contract_known)
        self.assertEqual(FactEvent.objects.count(), 1)
        decide(candidate.id)
        self.assertEqual(FinancialRecord.objects.count(), 1)

    def test_approximate_receipt_is_kept_separately_from_exact_totals(self):
        result = decide(self.candidate("По объекту 343 поступило почти 42 млн тенге", amount="42000000").id)
        self.assertEqual(result.outcome, "accepted")
        record = FinancialRecord.objects.get()
        self.assertEqual(record.amount_precision, "approximate")
        self.project.refresh_from_db()
        self.assertEqual(self.project.paid_amount, 0)
        self.assertEqual(financial_summary(Project.objects.all())["totals"][0]["amount_precision"], "approximate")

    def test_dotted_thousands_do_not_become_decimal_or_partial_amount(self):
        result = decide(self.candidate("По объекту 343 поступило 5.500.000 тенге", amount="5500000").id)
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(FinancialRecord.objects.get().amount, Decimal("5500000"))
        wrong = decide(self.candidate("По объекту 343 поступило 8.500.000 тенге", amount="500000").id)
        self.assertEqual(wrong.reason_code, "unknown_amount")

    def test_payment_date_cannot_be_invented_by_model(self):
        result = decide(self.candidate(payment_date='2010-01-01').id)
        self.assertEqual(result.reason_code, 'unsupported_payment_date')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_payment_plan_without_quoted_date_does_not_create_calendar_entry(self):
        from .models import PaymentScheduleItem
        result = decide(self.candidate('По объекту 343 ожидаем оплату 117 млн тенге', payment_kind='promise', payment_date='2027-01-01').id)
        self.assertEqual(result.outcome, 'accepted')
        self.assertFalse(PaymentScheduleItem.objects.exists())
        event = FactEvent.objects.get()
        self.assertIsNone(event.payload['payment_date'])
        self.assertTrue(event.payload['uncertainties'])

    def test_invented_exact_clock_for_tomorrow_is_downgraded_to_date(self):
        candidate = self.candidate('По объекту 343 завтра предоставлю цену', kind='commitment', commitment_text='Предоставить цену', responsible_name='Менеджер', assignment_kind='promise', commitment_status='pending', deadline_at=(timezone.now() + timedelta(days=1)).replace(hour=18).isoformat(), deadline_precision='datetime')
        raw = candidate.trace.raw_message
        candidate.proposed_changes.update(promise_message_id=raw.id, evidence_messages=[{'raw_message_id': raw.id, 'quote': raw.content, 'role': 'promise'}])
        candidate.save()
        self.assertEqual(decide(candidate.id).outcome, 'accepted')
        commitment = Commitment.objects.get()
        self.assertEqual(commitment.deadline_precision, 'date')


    def test_expense_does_not_count_as_sales_receipts(self):
        result = decide(self.candidate("По объекту 343 перечислили поставщику 117 млн тенге", direction="expense").id)
        self.assertEqual(result.outcome, "accepted")
        self.project.refresh_from_db()
        self.assertEqual(self.project.paid_amount, 0)

    def test_plan_not_materialized_as_receipt(self):
        result = decide(self.candidate("По объекту 343 оплатим 117 млн тенге завтра").id)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.reason_code, "plan_not_actual")
        self.assertFalse(FinancialRecord.objects.exists())

    def test_balance_is_recorded_as_observation_without_increasing_cash(self):
        result = decide(self.candidate("По объекту 343 на счете почти 42 млн тенге", amount="42000000", payment_kind="balance").id)
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(FactEvent.objects.get().event_type, "reported_balance")
        self.assertFalse(FinancialRecord.objects.exists())

    def test_possible_duplicate_is_deferred_instead_of_counted_twice(self):
        decide(self.candidate().id)
        duplicate = self.candidate()
        result = decide(duplicate.id)
        self.assertEqual(result.outcome, "deferred")
        self.assertEqual(result.reason_code, "possible_duplicate")
        self.assertEqual(FinancialRecord.objects.count(), 1)
        self.assertEqual(decide(duplicate.id).id, result.id)

    def test_edited_message_with_another_amount_is_not_a_second_payment(self):
        original = self.candidate()
        original.trace.raw_message.source_revision = 'old'
        original.trace.raw_message.save()
        decide(original.id)
        edited = self.candidate('По объекту 343 поступило 118 млн тенге', amount='118000000')
        raw = edited.trace.raw_message
        raw.message_id = original.trace.raw_message.message_id
        raw.source_revision = 'new'
        raw.save()
        self.assertEqual(decide(edited.id).reason_code, 'source_revision_requires_correction')
        self.assertEqual(FinancialRecord.objects.count(), 1)

    def test_active_and_archived_same_name_cannot_be_resolved_by_activity(self):
        active = Project.objects.create(team=self.team, name='Norex', normalized_name='norex', identity_confirmed=True)
        Project.objects.create(team=self.team, name='Norex', normalized_name='norex', archived=True, identity_confirmed=True)
        for suggested in (None, active):
            candidate = self.candidate('По Norex поступило 117 млн тенге', object_name='Norex')
            candidate.project = suggested
            candidate.save()
            self.assertEqual(decide(candidate.id).reason_code, 'ambiguous_project')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_blank_object_name_does_not_authorize_weak_project_suggestion(self):
        candidate = self.candidate('Поступило 117 млн тенге', object_name='')
        self.assertEqual(decide(candidate.id).reason_code, 'ambiguous_project')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_payment_amount_is_not_a_numeric_project_identity(self):
        candidate = self.candidate('Поступило 343 млн тенге', amount='343000000')
        self.assertEqual(decide(candidate.id).reason_code, 'ambiguous_project')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_missing_quote_and_foreign_team_cannot_be_applied(self):
        candidate = self.candidate()
        candidate.evidence.update(quote="Несуществующая цитата")
        result = decide(candidate.id)
        self.assertEqual(result.outcome, "deferred")
        self.assertFalse(FinancialRecord.objects.exists())
        other = self.candidate()
        other.team = Team.objects.create(name="Private")
        other.save()
        result = decide(other.id)
        self.assertEqual(result.outcome, "deferred")
        self.assertFalse(FinancialRecord.objects.exists())

    def test_preview_does_not_create_decisions_or_records(self):
        candidate = self.candidate()
        self.assertEqual(decide(candidate.id, preview=True)["outcome"], "accepted")
        self.assertFalse(FactDecision.objects.exists())
        self.assertFalse(FinancialRecord.objects.exists())

    def test_contract_value_requires_literal_number_and_contract_basis(self):
        valid = self.candidate("Договор по объекту 343 на 150 млн тенге", kind="project", contract_amount="150000000")
        self.assertEqual(decide(valid.id).outcome, "accepted")
        self.project.refresh_from_db()
        self.assertTrue(self.project.contract_known)
        wrong = self.candidate("План по объекту 343 на 200 млн тенге", kind="project", contract_amount="200000000")
        self.assertEqual(decide(wrong.id).reason_code, "plan_not_contract")
        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_amount, Decimal("150000000"))

    def test_new_project_created_without_amount_after_no_crm_match(self):
        candidate = self.candidate("Новый объект ЖК Ромашка, начинаем сбор ТЗ", kind="project", object_name="ЖК Ромашка")
        candidate.project = None
        candidate.base_project_version = 0
        candidate.crm_match_state = "not_found"
        candidate.save()
        result = decide(candidate.id)
        self.assertEqual(result.outcome, "accepted")
        project = Project.objects.get(name="ЖК Ромашка")
        self.assertFalse(project.contract_known)
        self.assertTrue(project.identity_confirmed)

    def test_unsupported_new_project_name_is_deferred(self):
        candidate = self.candidate("Обсудили работы", kind="project", object_name="Придуманный объект")
        candidate.project = None
        candidate.save()
        self.assertEqual(decide(candidate.id).reason_code, "ambiguous_project")

    def test_scoped_read_api_requires_authentication(self):
        api = APIClient()
        self.assertEqual(api.get("/api/autonomous/overview/").status_code, 401)
        user = User.objects.create_user("lead")
        TeamMembership.objects.create(user=user, team=self.team, role="team_lead", status="active")
        decide(self.candidate().id)
        api.force_authenticate(user)
        self.assertEqual(Decimal(api.get("/api/autonomous/overview/").data["totals"][0]["amount"]), Decimal("117000000"))
        stranger = User.objects.create_user("stranger")
        api.force_authenticate(stranger)
        self.assertEqual(api.get("/api/autonomous/overview/").data["totals"], [])

    def test_public_review_cannot_use_system_actor(self):
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            review(self.candidate().id, None, "approve")

    def test_checkpoint_does_not_skip_an_earlier_failed_message(self):
        first = self.candidate().trace.raw_message
        first.processing_state = "failed"
        first.save()
        last = self.candidate().trace.raw_message
        last.processed = True
        last.save()
        refresh_checkpoint(last)
        checkpoint = SourceCheckpoint.objects.get()
        self.assertIsNone(checkpoint.complete_through)
        self.assertEqual(checkpoint.gaps[0]["raw_message_id"], first.id)

    def test_report_is_created_automatically_and_idempotently(self):
        decide(self.candidate().id)
        refresh_reports()
        refresh_reports()
        self.assertEqual(FactEvent.objects.filter(event_type="daily_report").count(), 1)

    def test_duplicate_company_titles_are_allowed(self):
        Company.objects.create(name="Одинаковая компания", bitrix_company_id="1")
        Company.objects.create(name="Одинаковая компания", bitrix_company_id="2")
        self.assertEqual(Company.objects.count(), 2)

    def test_crm_patch_omits_unknown_contract(self):
        self.cfg.autonomous_crm_enabled = True
        self.cfg.save()
        BitrixSettings.objects.create(name="CRM", is_active=True)
        self.project.bitrix_id = "718"
        self.project.save()
        decide(self.candidate().id)
        delivery = CrmDelivery.objects.get(external_object_link__object_type="deal")
        self.assertNotIn("OPPORTUNITY", delivery.patch)
        self.assertNotIn("STAGE_ID", delivery.patch)
        with patch("api.bitrix_service.BitrixService.call", return_value={"result": {"TITLE": self.project.name}}) as call:
            deliver(delivery.outbox_event.payload)
        self.assertEqual(call.call_args.args[0], "crm.deal.get")
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "delivered")

    def test_old_crm_command_is_superseded_without_network(self):
        self.cfg.autonomous_crm_enabled = True
        self.cfg.save()
        BitrixSettings.objects.create(name="CRM", is_active=True)
        decide(self.candidate().id)
        delivery = CrmDelivery.objects.get(external_object_link__object_type="deal")
        Project.objects.filter(pk=self.project.id).update(version=delivery.target_version + 1)
        with patch("api.bitrix_service.BitrixService.call") as call:
            deliver(delivery.outbox_event.payload)
        call.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "superseded")

    @override_settings(BITRIX_STAGE_MAP={})
    def test_unmapped_stage_is_visible_and_retries_after_mapping_appears(self):
        from .providers import ProviderUnavailable
        from .autonomous import reconcile
        self.cfg.autonomous_crm_enabled = True
        self.cfg.save()
        BitrixSettings.objects.create(name='CRM', is_active=True)
        self.project.bitrix_id = '718'
        self.project.status = 'design'
        self.project.whatsapp_fields = ['stage']
        self.project.save()
        decide(self.candidate().id)
        delivery = CrmDelivery.objects.get(external_object_link__object_type='deal')
        with patch('api.bitrix_service.BitrixService.call', return_value={'result': {}}):
            with self.assertRaises(ProviderUnavailable) as caught:
                deliver(delivery.outbox_event.payload)
        self.assertEqual(str(caught.exception), 'crm_stage_mapping_missing')
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, 'partial')
        OutboxEvent.objects.filter(pk=delivery.outbox_event_id).update(state='failed')
        with override_settings(BITRIX_STAGE_MAP={'design': 'UC_DESIGN'}):
            reconcile()
            delivery.outbox_event.refresh_from_db()
            self.assertEqual(delivery.outbox_event.state, 'pending')
            with patch('api.bitrix_service.BitrixService.call', return_value={'result': {}}) as call:
                deliver(delivery.outbox_event.payload)
            self.assertEqual(call.call_args.args[0], 'crm.deal.update')
            self.assertEqual(call.call_args.args[1]['fields']['STAGE_ID'], 'UC_DESIGN')
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, 'delivered')

    def promise(self):
        candidate = self.candidate("По объекту 343 Вячеслав, прошу завтра найти клапаны", kind="commitment", commitment_text="Найти клапаны", assignment_kind="assignment", responsible_name="Вячеслав", commitment_status="pending")
        raw = candidate.trace.raw_message
        candidate.proposed_changes.update(promise_message_id=raw.id, evidence_messages=[{"raw_message_id": raw.id, "quote": raw.content, "role": "promise"}])
        candidate.save()
        candidate.evidence.update(field_name="promise")
        return candidate

    def test_obligation_created_without_crm_account(self):
        result = decide(self.promise().id)
        self.assertEqual(result.outcome, "accepted")
        task = Commitment.objects.get()
        self.assertEqual(task.responsible_name, "Вячеслав")
        self.assertIsNone(task.manager_id)
        self.assertEqual(task.participant.display_name, "Вячеслав")

    def update_promise(self, original, task, text, status, role):
        candidate = self.candidate(text, kind="commitment", commitment_text="Найти клапаны", assignment_kind="assignment", responsible_name="Вячеслав", commitment_status=status, commitment_id=task.id, base_commitment_version=task.version)
        raw = candidate.trace.raw_message
        promise = original.trace.raw_message
        candidate.proposed_changes.update(promise_message_id=promise.id, evidence_messages=[{"raw_message_id": promise.id, "quote": promise.content, "role": "promise"}, {"raw_message_id": raw.id, "quote": raw.content, "role": role}])
        if status == "fulfilled":
            candidate.proposed_changes["fulfillment_message_id"] = raw.id
        candidate.save()
        candidate.evidence.update(field_name=role)
        FactEvidence.objects.create(candidate=candidate, raw_message=promise, quote=promise.content, field_name="promise")
        return candidate

    def test_fulfillment_updates_original_task_instead_of_creating_another(self):
        original = self.promise()
        decide(original.id)
        task = Commitment.objects.get()
        update = self.update_promise(original, task, "Клапаны найдены, цена и срок переданы", "fulfilled", "fulfillment")
        result = decide(update.id)
        self.assertEqual(result.outcome, "accepted", result.explanation)
        task.refresh_from_db()
        self.assertEqual(task.status, "fulfilled")
        self.assertEqual(Commitment.objects.count(), 1)
        self.assertEqual(task.version, 2)

    def test_partial_fulfillment_cannot_close_whole_task(self):
        original = self.promise()
        decide(original.id)
        task = Commitment.objects.get()
        update = self.update_promise(original, task, "Клапаны найдены частично, остальные еще ищем", "fulfilled", "fulfillment")
        self.assertEqual(decide(update.id).reason_code, "partial_execution")
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")

    def test_cancellation_requires_cancellation_evidence(self):
        original = self.promise()
        decide(original.id)
        task = Commitment.objects.get()
        update = self.update_promise(original, task, "Поиск клапанов отменен, больше не нужно", "cancelled", "cancellation")
        result = decide(update.id)
        self.assertEqual(result.outcome, "accepted", result.explanation)
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")

    def test_concurrent_quota_reservation_is_bounded(self):
        from .provider_reservations import reserve
        from .providers import ProviderUnavailable
        self.cfg.daily_request_limit = 1
        self.cfg.save()
        reserve(self.cfg, {"model": "test"}, "chat")
        with self.assertRaises(ProviderUnavailable) as caught:
            reserve(self.cfg, {"model": "test"}, "chat")
        self.assertEqual(str(caught.exception), "ai_daily_budget_exhausted")

    def test_provider_circuit_backs_off_after_repeated_failures(self):
        from .provider_reservations import reserve
        from .providers import ProviderUnavailable
        from .models import ProviderUsage
        for _ in range(3):
            ProviderUsage.objects.create(operation="chat", model_name="test", duration_ms=1, succeeded=False, error_code="provider_server_error")
        with self.assertRaises(ProviderUnavailable) as caught:
            reserve(self.cfg, {"model": "test"}, "chat")
        self.assertEqual(str(caught.exception), "provider_circuit_open")

    def enable_crm(self):
        self.cfg.autonomous_crm_enabled = True
        self.cfg.save()
        return BitrixSettings.objects.create(name="CRM", is_active=True)

    def test_negated_fulfillment_never_closes_obligation(self):
        original = self.promise()
        decide(original.id)
        task = Commitment.objects.get()
        update = self.update_promise(original, task, "Клапаны не найдены, еще ищем", "fulfilled", "fulfillment")
        self.assertEqual(decide(update.id).reason_code, "fulfillment_unproven")
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")

    def test_preview_reports_domain_conflict_and_rolls_back_all_projections(self):
        candidate = self.candidate("По объекту 343 поступило 117 млн тенге", kind="project", contract_amount="0")
        with patch("api.autonomous._apply_candidate", side_effect=Conflict("Conflict")):
            # Use a valid project event so the application, not source validation,
            # exercises the failing transaction boundary.
            candidate.proposed_changes.pop("contract_amount")
            candidate.save()
            self.assertEqual(decide(candidate.id, preview=True)["reason_code"], "application_conflict")
        self.assertFalse(FactDecision.objects.exists())
        self.assertFalse(FactEvent.objects.exists())
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, "pending")

    def test_provider_outage_is_probed_after_quarantine(self):
        from .autonomous import reconcile
        event = OutboxEvent.objects.create(event_type="extract_message", deduplication_key="outage", payload={}, state="failed", error_code="provider_server_error", attempt_count=5, next_attempt_at=timezone.now() - timedelta(hours=2))
        reconcile()
        event.refresh_from_db()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt_count, 0)

    def test_pending_old_source_does_not_block_live_dispatch(self):
        from .tasks import dispatch_outbox
        for number in range(4):
            OutboxEvent.objects.create(event_type="extract_message", deduplication_key=f"old:{number}", payload={"priority": "history"})
        live = OutboxEvent.objects.create(event_type="extract_message", deduplication_key="live", payload={"priority": "live"})
        with patch("api.tasks.async_task") as queue:
            dispatch_outbox()
        self.assertEqual(queue.call_args_list[0].args[1], live.id)
        self.assertEqual(OutboxEvent.objects.filter(state="enqueued").count(), 2)

    def test_truncated_batch_uses_new_trace_and_smaller_packet(self):
        from .ai_retries import handle_failure
        from .providers import ProviderUnavailable
        raw = self.candidate().trace.raw_message
        event = OutboxEvent.objects.create(event_type="extract_message", deduplication_key="packet", payload={"raw_id": raw.id, "batch_ids": [raw.id, 123, 124, 125]}, attempt_count=1)
        handle_failure(event, ProviderUnavailable("provider_output_truncated"))
        event.refresh_from_db()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.payload["batch_ids"], [raw.id, 123])
        self.assertNotEqual(event.payload["trace_id"], raw.traces.order_by("id").first().id)

    def test_timeline_reconciles_uncertain_add_without_duplicate(self):
        self.enable_crm()
        self.project.bitrix_id = "718"
        self.project.save()
        decide(self.candidate().id)
        delivery = CrmDelivery.objects.get(external_object_link__object_type="timeline")
        marker = f"[MAZORY-EVENT:{delivery.fact_event_id}]"
        with patch("api.bitrix_service.BitrixService.call", return_value={"result": [{"ID": "111", "COMMENT": marker}]}) as call:
            deliver(delivery.outbox_event.payload)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], "crm.timeline.comment.list")
        delivery.refresh_from_db()
        self.assertEqual(delivery.external_object_link.external_id, "111")

    def test_task_without_external_assignee_keeps_local_obligation(self):
        from .providers import ProviderUnavailable
        self.enable_crm()
        self.project.bitrix_id = "718"
        self.project.save()
        decide(self.promise().id)
        delivery = CrmDelivery.objects.get(external_object_link__object_type="task")
        with patch("api.bitrix_service.BitrixService.call", return_value={"result": {"tasks": []}}) as call:
            with self.assertRaises(ProviderUnavailable) as caught:
                deliver(delivery.outbox_event.payload)
        self.assertEqual(str(caught.exception), "blocked_missing_external_assignee")
        self.assertEqual(call.call_count, 1)
        self.assertTrue(Commitment.objects.get().is_verified)
        self.assertIsNone(Commitment.objects.get().bitrix_task_id)

    def test_unknown_media_is_a_coverage_gap(self):
        from .message_artifacts import register
        from .models import MessageArtifact
        raw = self.candidate().trace.raw_message
        raw.raw_payload = {"payload": {"hasMedia": True, "media": None}}
        raw.processed, raw.processing_state = True, "analyzed"
        raw.save()
        register(raw)
        register(raw)
        self.assertEqual(MessageArtifact.objects.count(), 1)
        refresh_checkpoint(raw)
        self.assertEqual(SourceCheckpoint.objects.get().gaps[0]["state"], "missing_media")

    @override_settings(WAHA_API_URL="http://waha:3000", WAHA_API_KEY="test")
    def test_media_download_refuses_unrelated_hosts_and_path_traversal(self):
        from .tasks import waha_download_media
        from .providers import ProviderUnavailable
        with patch("api.tasks.requests.get") as get:
            for url in ["https://evil.test/api/files/a", "http://waha:3000/api/files/%2e%2e/secret", "http://waha:3000/api/files/a?x=1"]:
                with self.assertRaises(ProviderUnavailable):
                    waha_download_media(url)
        get.assert_not_called()

    @override_settings(OCR_URL="https://recognizer.test/ocr")
    def test_recognized_media_preserves_original_and_has_derived_provenance(self):
        from .message_artifacts import register, process
        from .models import MessageArtifact, ProviderUsage
        raw = self.candidate().trace.raw_message
        raw.raw_payload = {"payload": {"hasMedia": True, "media": {"url": "http://waha:3000/api/files/a", "mimetype": "image/png"}}}
        raw.save()
        original = raw.content
        register(raw)
        with patch("api.tasks.waha_download_media", return_value=b"\x89PNG\r\n\x1a\nbody"), patch("api.message_artifacts.requests.post") as post:
            post.return_value.json.return_value = {"text": "По объекту 343 поступило 117 млн тенге"}
            process(MessageArtifact.objects.get().id)
            process(MessageArtifact.objects.get().id)
        self.assertEqual(post.call_count, 1)
        raw.refresh_from_db()
        self.assertEqual(raw.content, original)
        derived = RawMessage.objects.exclude(pk=raw.pk).get()
        self.assertEqual(derived.raw_payload["original_raw_id"], raw.id)
        self.assertEqual(ProviderUsage.objects.get().operation, "ocr")

    def test_company_not_in_citations_is_not_invented(self):
        result = decide(self.candidate(company_name="Придуманная компания").id)
        self.assertEqual(result.outcome, "accepted")
        self.assertIn("company_name", result.validation["discarded_fields"])
        self.assertFalse(Company.objects.exists())

    def test_month_plan_and_debt_are_not_sales(self):
        for amount, kind, text in [("171471000", "promise", "По объекту 343 план сбора на октябрь 171,471 млн тенге"), ("50874000", "debt", "По объекту 343 долг 50,874 млн тенге")]:
            result = decide(self.candidate(text, amount=amount, payment_kind=kind, payment_date=None).id)
            self.assertEqual(result.outcome, "accepted", result.explanation)
        self.assertFalse(FinancialRecord.objects.exists())
        self.assertEqual(FactEvent.objects.count(), 2)

    def test_historical_report_is_not_a_new_obligation(self):
        candidate = self.candidate("По объекту 343 вчера провел встречу, согласовал поставку", kind="commitment", commitment_text="Согласовать поставку", responsible_name="Боб", assignment_kind="assignment")
        self.assertIn(decide(candidate.id).reason_code, ["historical_action", "not_an_assignment"])
        self.assertFalse(Commitment.objects.exists())

    def test_reports_do_not_expose_staff_finances_to_client(self):
        from .models import ClientProjectAccess
        client = User.objects.create_user("client")

        ClientProjectAccess.objects.create(user=client, project=self.project, status="active")
        api = APIClient()
        api.force_authenticate(client)
        self.assertEqual(api.get("/api/autonomous/overview/").status_code, 403)

    def test_all_members_of_batch_must_be_classified_before_marking_processed(self):
        from .pipeline import extract_message
        from .tests_dialogue_threads import Counter
        from .providers import ProviderUnavailable
        rows = [self.candidate(text=f"Обсуждение {i}").trace.raw_message for i in range(2)]
        endpoint = {"tag": "test", "api_format": "openai_compatible", "effective_provider_url": self.cfg.chat_provider_url, "context_length": 256000}
        result = {"threads": [{"key": "discussion", "topic": "Обсуждение", "state": "open", "messages": [{"raw_message_id": rows[0].id, "thought_state": "intermediate", "relation": "discusses"}]}], "facts": []}
        with patch("api.pipeline.AIService._config", return_value=self.cfg), patch("api.message_context.context_runtime", return_value=(Counter(), endpoint)), patch("api.pipeline.AIService.analyze_payload", return_value=(result, {}, {})):
            with self.assertRaises(ProviderUnavailable) as caught:
                extract_message(rows[0].id, batch_ids=[row.id for row in rows])
        self.assertEqual(str(caught.exception), "batch_classification_missing")
        self.assertFalse(RawMessage.objects.filter(processed=True).exists())

    def test_wrong_existing_project_cannot_be_authorized_by_extracted_label(self):
        for source_label, wrong_project in [('ПГУ ЗРУ Жезказган', 'ФОК Степногорск'), ('Дымовые трубы Diamond', 'БТП Diamond')]:
            target = Project.objects.create(team=self.team, name=wrong_project, identity_confirmed=True)
            candidate = self.candidate(f'По {source_label} поступило 117 млн тенге', object_name=source_label)
            candidate.project = target
            candidate.save()
            self.assertEqual(decide(candidate.id).reason_code, 'ambiguous_project')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_future_and_commercial_offer_never_create_cash(self):
        for text in ['По объекту 343 перечислим 117 млн тенге', 'По объекту 343 получим 117 млн тенге', 'По объекту 343 согласовали получение 117 млн тенге', 'По объекту 343 получил коммерческое предложение на 117 млн тенге']:
            self.assertEqual(decide(self.candidate(text).id).outcome, 'rejected')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_currency_cannot_be_changed_from_tenge_to_dollars(self):
        result = decide(self.candidate(currency='USD').id)
        self.assertEqual(result.reason_code, 'currency_conflict')
        self.assertFalse(FinancialRecord.objects.exists())

    def test_unknown_provider_usage_still_consumes_reserved_token_budget(self):
        from .provider_reservations import reserve
        from .models import ProviderUsage
        from .providers import ProviderUnavailable
        self.cfg.autonomous_daily_token_limit = 9000
        self.cfg.save()
        reservation = reserve(self.cfg, {'model': 'test'}, 'chat')
        reservation.usage = ProviderUsage.objects.create(operation='chat', model_name='test', duration_ms=1, succeeded=True)
        reservation.state = 'settled'
        reservation.save()
        with self.assertRaises(ProviderUnavailable) as caught:
            reserve(self.cfg, {'model': 'test'}, 'chat')
        self.assertEqual(str(caught.exception), 'ai_daily_budget_exhausted')

    def test_interrupted_worker_records_provider_reason_and_keeps_quota(self):
        from .ai_service import AIService
        from .models import ProviderUsage, ProviderReservation
        with patch('api.ai_service.requests.post', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                AIService._post('https://provider.example/v1/chat/completions', {'model': 'synthetic'}, 1, api_key='test')
        usage = ProviderUsage.objects.get()
        self.assertFalse(usage.succeeded)
        self.assertEqual(usage.error_code, 'provider_interrupted')
        self.assertEqual(ProviderReservation.objects.get().usage_id, usage.id)

    def test_reported_vendor_promise_is_not_assigned_to_message_author(self):
        candidate = self.candidate('По объекту 343 Дмитрий обещал завтра предоставить цену', kind='commitment', commitment_text='Предоставить цену', responsible_name='Дмитрий', assignment_kind='reported_promise', commitment_status='pending')
        raw = candidate.trace.raw_message
        candidate.proposed_changes.update(promise_message_id=raw.id, evidence_messages=[{'raw_message_id': raw.id, 'quote': raw.content, 'role': 'promise'}])
        candidate.save()
        self.assertEqual(decide(candidate.id).outcome, 'accepted')
        self.assertEqual(Commitment.objects.get().responsible_name, 'Дмитрий')
        self.assertIsNone(Commitment.objects.get().manager_id)

    def test_reported_promise_cannot_invent_unquoted_surname(self):
        candidate = self.candidate('По объекту 343 Дмитрий обещал предоставить цену', kind='commitment', commitment_text='Предоставить цену', responsible_name='Дмитрий Иванов', assignment_kind='reported_promise', commitment_status='pending')
        raw = candidate.trace.raw_message
        candidate.proposed_changes.update(promise_message_id=raw.id, evidence_messages=[{'raw_message_id': raw.id, 'quote': raw.content, 'role': 'promise'}])
        candidate.save()
        self.assertEqual(decide(candidate.id).reason_code, 'unknown_participant')
        self.assertFalse(Commitment.objects.exists())

    def test_plan_to_complete_project_does_not_change_stage_to_completed(self):
        candidate = self.candidate('По объекту 343 планируем завершить проект завтра', kind='project', stage='completed')
        self.assertEqual(decide(candidate.id).reason_code, 'unsupported_stage')
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.status, 'completed')

    def test_exact_crm_object_can_create_reference_without_importing_opportunity(self):
        from .models import CandidateCrmMatch
        candidate = self.candidate('По объекту Север завершен сбор ТЗ', kind='project', object_name='Объект Север')
        candidate.project = None
        candidate.crm_match_revision = 1
        candidate.crm_match_state = 'matched'
        candidate.save()
        CandidateCrmMatch.objects.create(candidate=candidate, crm_match_revision=1, bitrix_deal_id='999', deal_title='CRM Север', object_label='Объект Север', opportunity=Decimal('250000000'), currency='KZT')
        result = decide(candidate.id)
        self.assertEqual(result.outcome, 'accepted', result.explanation)
        project = Project.objects.get(bitrix_id='999')
        self.assertEqual(project.contract_amount, 0)
        self.assertFalse(project.contract_known)
        self.assertEqual(project.whatsapp_fields, [])
