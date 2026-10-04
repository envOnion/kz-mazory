from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone

from .admin import MessageProcessingTraceAdmin
from .history_jobs import persist_items, progress, process_step, start_job
from .job_admin import WhatsAppHistoryRunAdmin
from .models import (
    AISettings, Company, DialogueThread, FactCandidate, MessageProcessingTrace,
    OutboxEvent, Project, RawMessage, Team, ThreadMessage, ThreadRevision,
    WhatsAppConfig, WhatsAppHistoryItem, WhatsAppHistoryJob, WhatsAppHistoryRun,
)
from .tasks import run_outbox


class HistoryReprocessingTests(TestCase):
    def setUp(self):
        self.config = WhatsAppConfig.objects.create(
            team=Team.objects.create(name="History"), group_jid="test@g.us",
        )
        self.job = WhatsAppHistoryJob.objects.create(config=self.config)

    def run_record(self, **settings):
        return WhatsAppHistoryRun.objects.create(
            job=self.job, state="completed", source_snapshot={
                "config_id": self.config.id, "team_id": self.config.team_id,
                "session_name": "default", "chat_id": "test@g.us",
            }, settings_snapshot={"only_new": False, "analyze_after_import": True,
                "analysis_mode": "reprocess_all", "poll_seconds": 1, **settings},
        )

    def item(self, run, mid="m1", text="Оплата получена"):
        return WhatsAppHistoryItem(run=run, message_id=mid, timestamp=timezone.now(),
            payload={"id": mid, "body": text}, seen_scan=1)

    def persist(self, run, items):
        persist_items(run, self.config, items)
        run.save()

    def test_full_run_reprocesses_success_failed_and_new_idempotently(self):
        first = self.run_record()
        self.persist(first, [self.item(first, "ok"), self.item(first, "failed")])
        raws = list(first.messages.order_by("id"))
        RawMessage.objects.filter(pk=raws[0].id).update(processed=True, processing_state="no_facts")
        RawMessage.objects.filter(pk=raws[1].id).update(processing_state="failed")
        OutboxEvent.objects.filter(payload__history_run_id=first.id).update(state="failed")
        for raw in raws:
            OutboxEvent.objects.create(event_type="extract_message",
                deduplication_key=f"extract:{raw.id}", payload={"raw_id": raw.id}, state="failed")
        second = self.run_record()
        items = [self.item(second, "ok"), self.item(second, "failed"), self.item(second, "new")]
        self.persist(second, items)
        self.persist(second, items)
        self.assertEqual(RawMessage.objects.count(), 3)
        self.assertEqual(second.scheduled_count, 3)
        self.assertEqual(second.existing_count, 2)
        self.assertEqual(second.imported_count, 1)
        self.assertEqual(progress(second), {"total": 3, "processed": 0, "errors": 0, "pending": 3})
        events = OutboxEvent.objects.filter(payload__history_run_id=second.id)
        self.assertEqual(events.count(), 3)
        for event in events:
            self.assertTrue(MessageProcessingTrace.objects.filter(pk=event.payload["trace_id"]).exists())
        event = events.first()
        MessageProcessingTrace.objects.filter(pk=event.payload["trace_id"]).update(status="success")
        events.filter(pk=event.pk).update(state="done")
        events.exclude(pk=event.pk).update(state="failed")
        self.assertEqual(progress(second), {"total": 3, "processed": 1, "errors": 2, "pending": 0})

    def test_only_new_preserves_existing_message_skip(self):
        first = self.run_record()
        self.persist(first, [self.item(first)])
        raw = first.messages.get()
        RawMessage.objects.filter(pk=raw.pk).update(processed=True)
        second = self.run_record(only_new=True, analysis_mode="incremental")
        self.persist(second, [self.item(second)])
        self.assertEqual(second.scheduled_count, 0)

    def test_no_text_and_import_without_analysis(self):
        run = self.run_record(analyze_after_import=False)
        self.persist(run, [self.item(run, "empty", ""), self.item(run, "text")])
        self.assertEqual(run.no_text_count, 1)
        self.assertEqual(run.scheduled_count, 0)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_start_snapshots_full_reprocessing_mode(self):
        run = start_job(self.job.id)
        self.assertEqual(run.settings_snapshot["analysis_mode"], "reprocess_all")

    def test_worker_waits_for_older_attempt_without_consuming_retry(self):
        first = self.run_record()
        self.persist(first, [self.item(first)])
        second = self.run_record()
        self.persist(second, [self.item(second)])
        event = OutboxEvent.objects.get(payload__history_run_id=second.id)
        with patch("api.tasks.AISettings.get_active", return_value=AISettings(message_processing_paused=False)), patch("api.tasks.extract_message") as extract:
            run_outbox(event.id)
        extract.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt_count, 0)

    def test_more_than_2000_messages_and_short_pages_continue_to_empty(self):
        run = self.run_record(page_size=1000, stable_scans_required=2)
        run.state = "collecting"
        run.cutoff_at = timezone.now()
        run.save()
        timestamp = int((timezone.now() - timedelta(hours=1)).timestamp())
        messages = [{"id": f"m{i}", "body": "Текст", "timestamp": timestamp, "from": "test@g.us"} for i in range(2101)]
        pages = [messages[:1000], messages[1000:1500], messages[1500:], []]
        offsets = []
        for scan in range(2):
            for page in pages:
                run.refresh_from_db()
                offsets.append(run.offset)
                process_step({"history_run_id": run.id, "step": run.step}, lambda *a, **kw: page)
        run.refresh_from_db()
        self.assertEqual(offsets, [0, 1000, 1500, 2101] * 2)
        self.assertEqual(run.state, "importing")
        self.assertEqual(run.fetched_count, 2101)
        process_step({"history_run_id": run.id, "step": run.step}, None)
        run.refresh_from_db()
        self.assertEqual(run.messages.count(), 2101)
        self.assertEqual(run.scheduled_count, 2101)
        self.assertEqual(progress(run)["pending"], 2101)

    def test_explicit_attempt_reprocesses_success_and_preserves_approved_facts(self):
        first = self.run_record()
        self.persist(first, [self.item(first, text="Оплата 10 тенге получена по объекту")])
        raw = first.messages.get()
        old = raw.traces.get()
        old.status = "success"
        old.save()
        raw.processed = True
        raw.save()
        fact = {"fact_type": "payment", "object_name": "Объект", "evidence": raw.content,
                "confidence": "0.9", "uncertainties": [], "amount": "10", "currency": "KZT", "payment_kind": "increment"}
        approved = FactCandidate.objects.create(trace=old, team=self.config.team,
            fact_type="payment", proposed_changes=fact, source_key="approved", status="approved")
        pending = FactCandidate.objects.create(trace=old, team=self.config.team,
            fact_type="payment", proposed_changes=fact, source_key="pending")
        second = self.run_record()
        self.persist(second, [self.item(second, text="Оплата 10 тенге получена по объекту")])
        event = OutboxEvent.objects.get(payload__history_run_id=second.id)
        cfg = AISettings(chat_model_name="test")
        envelope = {"messages": [{"role": "user", "content": '{"context": []}'}]}
        result = {"threads": [{"key": "payment", "topic": "Оплата объекта", "state": "ready", "completion_reason": "Сообщено о получении платежа",
                               "messages": [{"raw_message_id": raw.id, "thought_state": "final"}]}],
                  "facts": [{**fact, "thread_key": "payment", "evidence_message_id": raw.id}]}
        from .pipeline import extract_message
        with patch("api.pipeline.AIService._config", return_value=cfg), patch(
            "api.pipeline.AIService.effective_chat_provider_url", return_value="https://example.com/v1"
        ), patch("api.pipeline.build_context", return_value=([], {"request_state": "not_sent"}, envelope)), patch(
            "api.pipeline.AIService.analyze_payload", return_value=(result, {}, {})
        ) as analyze:
            extract_message(raw.id, trace_id=event.payload["trace_id"])
            extract_message(raw.id, trace_id=event.payload["trace_id"])
        self.assertEqual(analyze.call_count, 1)
        approved.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(approved.status, "approved")
        self.assertEqual(pending.status, "superseded")
        self.assertEqual(FactCandidate.objects.count(), 2)
        self.assertEqual(raw.traces.get(pk=event.payload["trace_id"]).status, "success")

    def test_current_failed_attempt_finishes_even_if_old_message_was_successful(self):
        run = self.run_record()
        self.persist(run, [self.item(run)])
        RawMessage.objects.filter(pk=run.messages.get().id).update(processed=True)
        event = OutboxEvent.objects.get(payload__history_run_id=run.id)
        OutboxEvent.objects.filter(pk=event.id).update(state="failed", error_code="invalid_schema")
        run.state = "analyzing"
        run.save()
        process_step({"history_run_id": run.id, "step": run.step}, None)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed_with_errors")
        self.assertEqual(progress(run)["processed"], 0)

    def test_retry_stays_pending_and_active_newer_attempt_blocks_worker(self):
        first = self.run_record()
        self.persist(first, [self.item(first)])
        second = self.run_record()
        self.persist(second, [self.item(second)])
        older = OutboxEvent.objects.get(payload__history_run_id=first.id)
        newer = OutboxEvent.objects.get(payload__history_run_id=second.id)
        OutboxEvent.objects.filter(pk=newer.id).update(state="processing")
        with patch("api.tasks.AISettings.get_active", return_value=AISettings(message_processing_paused=False)), patch("api.tasks.extract_message") as extract:
            run_outbox(older.id)
        extract.assert_not_called()
        MessageProcessingTrace.objects.filter(pk=older.payload["trace_id"]).update(status="error")
        self.assertEqual(progress(first)["pending"], 1)
        self.assertEqual(progress(first)["errors"], 0)

    def test_cancelled_extractions_do_not_leave_monitor_waiting_forever(self):
        run = self.run_record()
        self.persist(run, [self.item(run)])
        OutboxEvent.objects.filter(payload__history_run_id=run.id).update(
            state="cancelled", error_code="replaced_by_thread_backfill",
        )
        run.state = "analyzing"
        run.save()
        self.assertEqual(progress(run), {"total": 1, "processed": 0, "errors": 1, "pending": 0})
        process_step({"history_run_id": run.id, "step": run.step}, None)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed_with_errors")
        self.assertIsNotNone(run.finished_at)

    def test_backfill_monitor_counts_its_events_not_old_successes(self):
        from django.contrib.auth.models import User
        from .models import TeamMembership
        from .thread_backfill import track_backfill
        from .processing_attempts import reserve_attempt

        user = User.objects.create_user(username="history-lead")
        TeamMembership.objects.create(user=user, team=self.config.team, role="team_lead", status="active")
        old = self.run_record()
        self.persist(old, [self.item(old, "text"), self.item(old, "empty", "")])
        raw = old.messages.exclude(content="").get()
        MessageProcessingTrace.objects.filter(raw_message=raw).update(status="success")
        RawMessage.objects.filter(pk=raw.id).update(processed=True)
        trace = reserve_attempt(raw, f"thread-rebuild:{user.id}:tracking:{raw.id}")
        event = OutboxEvent.objects.create(event_type="extract_message", deduplication_key=trace.operation_key,
            payload={"raw_id": raw.id, "trace_id": trace.id}, state="processing")
        other = RawMessage.objects.create(config=self.config, team=self.config.team, source="waha",
            session_name="default", chat_id="another@g.us", message_id="other", source_revision="r", content="Другой чат", timestamp=timezone.now())
        other_event = OutboxEvent.objects.create(event_type="extract_message",
            deduplication_key=f"thread-rebuild:{user.id}:tracking:{other.id}", payload={"raw_id": other.id})
        count = OutboxEvent.objects.count()
        run = track_backfill(user, self.job.id, "tracking")
        self.assertEqual(run.messages.count(), 2)
        self.assertEqual(progress(run), {"total": 2, "processed": 1, "errors": 0, "pending": 1})
        self.assertEqual(OutboxEvent.objects.count(), count + 1)  # monitor only; no duplicate AI
        self.assertEqual(track_backfill(user, self.job.id, "tracking").id, run.id)
        other_event.refresh_from_db()
        self.assertNotIn("history_run_id", other_event.payload)
        MessageProcessingTrace.objects.filter(pk=trace.id).update(status="success")
        OutboxEvent.objects.filter(pk=event.id).update(state="done")
        self.assertEqual(progress(run), {"total": 2, "processed": 2, "errors": 0, "pending": 0})
        process_step({"history_run_id": run.id, "step": run.step}, None)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")

    def test_thematic_tree_card_and_summary_render(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            team=self.config.team,
            company=company,
            bitrix_id="8920",
            contract_amount=15000000,
        )
        run = self.run_record()
        self.persist(run, [self.item(run, mid="m_tree", text="Поставка БТП на объект ЖК Grand Park")])
        raw = run.messages.get()
        trace = raw.traces.get()

        root = DialogueThread.objects.create(
            team=self.config.team,
            config=self.config,
            source_key="waha:test@g.us",
            identity="id_tree_root",
            topic="Поставка БТП",
            state="ready",
            project=project,
        )
        child = DialogueThread.objects.create(
            team=self.config.team,
            config=self.config,
            source_key="waha:test@g.us",
            identity="id_tree_child",
            parent=root,
            topic="Авансовый платеж",
            state="open",
            project=project,
        )
        ThreadMessage.objects.create(
            thread=root,
            raw_message=raw,
            thought_state="final",
            relation="discusses",
        )
        rev = ThreadRevision.objects.create(
            thread=root,
            version=1,
            state="ready",
            topic="Поставка БТП",
            summary="Смета и согласование",
        )
        FactCandidate.objects.create(
            trace=trace,
            thread_revision=rev,
            team=self.config.team,
            project=project,
            fact_type="commitment",
            proposed_changes={"commitment_text": "Поставить БТП до 10.10", "amount": 15000000, "deadline": "2026-10-10"},
            source_key="cand_tree_1",
            status="approved",
        )

        site = AdminSite()
        run_admin = WhatsAppHistoryRunAdmin(WhatsAppHistoryRun, site)

        summary = run_admin.thematic_summary(run)
        self.assertIn("тред", summary)
        self.assertIn("ready", summary)
        self.assertIn("обязательств", summary)

        tree_html = run_admin.thematic_tree_card(run)
        self.assertIn("ТОО BI Group", tree_html)
        self.assertIn("ЖК Grand Park", tree_html)
        self.assertIn("Поставка БТП", tree_html)
        self.assertIn("Авансовый платеж", tree_html)
        self.assertIn("15 000 000", tree_html)

        empty_run = self.run_record()
        self.assertEqual(run_admin.thematic_summary(empty_run), "0 тредов")
        empty_html = run_admin.thematic_tree_card(empty_run)
        self.assertIn("не распознаны", empty_html)

    def test_dialogue_thread_hierarchy_card_and_thread_badge_render(self):
        company = Company.objects.create(name="ТОО Bazis-A", bitrix_company_id="2048")
        project = Project.objects.create(
            name="БЦ Север",
            team=self.config.team,
            company=company,
            bitrix_id="5544",
            contract_amount=20000000,
        )
        run = self.run_record()
        self.persist(run, [self.item(run, mid="m_trace", text="Согласовали договор и аванс")])
        raw = run.messages.get()
        trace = raw.traces.get()
        trace.project = project
        trace.status = "success"
        trace.pipeline_action = "created_deal"
        trace.whatsapp_sender_name = "Нурлан"
        trace.whatsapp_sender_phone = "+77015551234"
        trace.ai_extracted_facts = {"object_name": "БЦ Север", "contract_amount": 20000000, "stage": "C1:PROPOSAL"}
        trace.save()

        thread = DialogueThread.objects.create(
            team=self.config.team,
            config=self.config,
            source_key="waha:test@g.us",
            identity="id_trace_thread",
            topic="Договор БЦ Север",
            state="open",
            project=project,
        )
        subthread = DialogueThread.objects.create(
            team=self.config.team,
            config=self.config,
            source_key="waha:test@g.us",
            identity="id_sub_trace",
            parent=thread,
            topic="График оплат",
            state="open",
            project=project,
        )
        ThreadMessage.objects.create(
            thread=thread,
            raw_message=raw,
            thought_state="intermediate",
            relation="discusses",
        )
        rev = ThreadRevision.objects.create(
            thread=thread,
            version=1,
            state="open",
            topic="Договор БЦ Север",
            summary="Обсуждение договора",
        )
        FactCandidate.objects.create(
            trace=trace,
            thread_revision=rev,
            team=self.config.team,
            project=project,
            fact_type="payment",
            proposed_changes={"payment_type": "Аванс", "amount": 5000000, "payment_date": "05.10.2026"},
            source_key="cand_trace_1",
            status="approved",
        )

        site = AdminSite()
        trace_admin = MessageProcessingTraceAdmin(MessageProcessingTrace, site)

        badge_html = trace_admin.thread_badge(trace)
        self.assertIn(f"Тред #{thread.id}", badge_html)
        self.assertIn("open", badge_html)
        self.assertIn("БЦ Север", badge_html)

        card_html = trace_admin.dialogue_thread_hierarchy_card(trace)
        self.assertIn("ТОО Bazis-A", card_html)
        self.assertIn("БЦ Север", card_html)
        self.assertIn("Договор БЦ Север", card_html)
        self.assertIn("График оплат", card_html)
        self.assertIn("5,000,000.00", card_html)
        self.assertIn("open (мысль в процессе)", card_html)

        empty_raw = RawMessage.objects.create(
            config=self.config,
            team=self.config.team,
            source="waha",
            session_name="default",
            chat_id="test@g.us",
            message_id="m_empty",
            source_revision="r",
            content="Автономное сообщение",
            timestamp=timezone.now(),
        )
        empty_trace = MessageProcessingTrace.objects.create(
            raw_message=empty_raw,
            status="success",
            pipeline_action="non_commercial",
            attempt_no=1,
        )
        empty_badge = trace_admin.thread_badge(empty_trace)
        self.assertIn("Вне треда", empty_badge)
        empty_card = trace_admin.dialogue_thread_hierarchy_card(empty_trace)
        self.assertIn("Сообщение ещё не включено в тред диалога", empty_card)

