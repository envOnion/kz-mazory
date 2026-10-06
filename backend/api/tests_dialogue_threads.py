import json
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from .models import (
    AISettings,
    BitrixSettings,
    CrmCatalogSync,
    DialogueThread,
    ThreadMessage,
    ThreadRevision,
    Project,
    RawMessage,
    FactCandidate,
    Team,
    TeamMembership,
    UserProfile,
    WhatsAppConfig,
    OutboxEvent,
    Company,
    ThreadSubscription,
    Notification,
)
from .pipeline import extract_message
from .crm_catalog import enqueue_catalog, sync_page
from .providers import ProviderUnavailable

CASES = json.loads(
    (Path(__file__).parent / "test_fixtures/dialogue_cases.json").read_text()
)


class Counter:
    remote = False
    strategy = "test_count"
    request_count = 0

    def count_payload(self, value):
        return len(json.dumps(value, ensure_ascii=False)) // 4

    def count_text(self, text):
        return len(text) // 4

    def offsets(self, text):
        return range(len(text))


def fixture_result(case, rows, known=()):
    topics = {
        "north": "Смета БЦ Север",
        "south": "Доставка БЦ Южный",
        "general": "Список объектов команды",
        "unknown": "Требует уточнения",
    }
    existing = {item["topic"]: item["id"] for item in known}
    threads, facts = [], []
    for key in dict.fromkeys(case["assignments"]):
        members = [
            row
            for row, assignment in zip(rows, case["assignments"])
            if assignment == key
        ]
        ready = key in case["ready"]
        threads.append(
            {
                "key": key,
                "thread_id": existing.get(topics[key]),
                "topic": topics[key],
                "state": "ready"
                if ready
                else "unknown"
                if key == "unknown"
                else "open",
                "summary": topics[key],
                "completion_reason": "Есть конкретная просьба и явное обещание"
                if ready
                else "",
                "messages": [
                    {
                        "raw_message_id": row.id,
                        "thought_state": "final"
                        if ready and row == members[-1]
                        else "intermediate",
                        "relation": "answers" if row != members[0] else "discusses",
                    }
                    for row in members
                ],
            }
        )
        if ready:
            promise = members[-1]
            facts.append(
                {
                    "thread_key": key,
                    "evidence_message_id": promise.id,
                    "fact_type": "commitment",
                    "object_name": "БЦ Север"
                    if key == "north"
                    else "БЦ Южный"
                    if key == "south"
                    else "",
                    "commitment_text": "Подготовить смету БЦ Север"
                    if key == "north"
                    else "Проверить доставку БЦ Южный"
                    if key == "south"
                    else "Подготовить список объектов команды",
                    "promise_message_id": promise.id,
                    "responsible_name": "Боб",
                    "evidence": promise.content,
                    "confidence": 0.95,
                    "evidence_messages": [
                        {
                            "raw_message_id": members[0].id,
                            "quote": members[0].content,
                            "role": "request",
                        },
                        {
                            "raw_message_id": promise.id,
                            "quote": promise.content,
                            "role": "promise",
                        },
                    ],
                }
            )
    return {"threads": threads, "facts": facts}


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class DialogueTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Threads")
        self.cfg = WhatsAppConfig.objects.create(
            team=self.team, group_jid="threads@g.us", snapshot={"timezone": "UTC+03:00"}
        )
        self.user = User.objects.create_user("lead")
        TeamMembership.objects.create(
            user=self.user, team=self.team, role="team_lead", status="active"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.ai = AISettings(
            chat_model_name="test",
            chat_provider_url="https://provider.test/v1",
            context_window_tokens=262144,
        )

    def messages(self, texts):
        return [
            RawMessage.objects.create(
                config=self.cfg,
                team=self.team,
                chat_id=self.cfg.group_jid,
                message_id=f"m:{RawMessage.objects.count()}",
                timestamp=timezone.now(),
                sender_name="Боб",
                content=text,
            )
            for text in texts
        ]

    def run_result(self, raw, result, trace_id=None):
        endpoint = {
            "tag": "test",
            "api_format": "openai_compatible",
            "effective_provider_url": self.ai.chat_provider_url,
            "context_length": 262144,
        }
        with (
            patch("api.pipeline.AIService._config", return_value=self.ai),
            patch(
                "api.message_context.context_runtime",
                return_value=(Counter(), endpoint),
            ),
            patch(
                "api.pipeline.AIService.analyze_payload", return_value=(result, {}, {})
            ),
        ):
            extract_message(raw.id, trace_id=trace_id)

    def test_queue_serializes_messages_of_one_source(self):
        from .tasks import run_outbox
        first, second = self.messages(["Первый вопрос", "Поздний ответ"])
        earlier = OutboxEvent.objects.create(
            event_type="extract_message", deduplication_key="earlier",
            payload={"raw_id": first.id},
        )
        later = OutboxEvent.objects.create(
            event_type="extract_message", deduplication_key="later",
            payload={"raw_id": second.id},
        )
        with patch("api.tasks.AISettings.get_active", return_value=self.ai), patch(
            "api.tasks.extract_message"
        ) as handler:
            run_outbox(later.id)
            handler.assert_not_called()
            later.refresh_from_db()
            self.assertEqual(later.state, "pending")
            earlier.state = "done"
            earlier.save(update_fields=["state"])
            later.next_attempt_at = timezone.now()
            later.save(update_fields=["next_attempt_at"])
            run_outbox(later.id)
            later.refresh_from_db()
            handler.assert_called_once_with(later.payload)
            later.refresh_from_db()
            self.assertEqual(later.state, "done")

    def test_history_wait_does_not_exhaust_retry_budget(self):
        from .tasks import run_outbox
        raw = self.messages(["Обсуждение"])[0]
        OutboxEvent.objects.create(
            event_type="extract_message", deduplication_key="active",
            payload={"raw_id": raw.id},
        )
        page = OutboxEvent.objects.create(
            event_type="thread_backfill", deduplication_key="waiting-page",
            payload={"config_id": self.cfg.id, "requested_by_id": self.user.id,
                     "request_key": "wait", "cursor": 0},
        )
        for _ in range(5):
            page.next_attempt_at = timezone.now()
            page.save(update_fields=["next_attempt_at"])
            run_outbox(page.id)
            page.refresh_from_db()
            self.assertEqual(page.state, "pending")
            self.assertEqual(page.attempt_count, 0)

    def test_history_page_enqueues_scoped_message_attempts(self):
        from .thread_backfill import backfill_page
        rows = self.messages(["Первая тема", "Ответ по теме"])
        payload = {"config_id": self.cfg.id, "requested_by_id": self.user.id,
                   "request_key": "scoped-page", "cursor": 0}
        backfill_page(payload)
        events = list(OutboxEvent.objects.filter(event_type="extract_message"))
        self.assertEqual({event.payload["raw_id"] for event in events},
                         {row.id for row in rows})
        for event in events:
            self.assertEqual(event.payload["requested_by_id"], self.user.id)
            self.assertEqual(RawMessage.objects.get(pk=event.payload["raw_id"]).traces.get(
                pk=event.payload["trace_id"]).prompt_version, "facts-v5-dialogue-threads")
        self.assertEqual(OutboxEvent.objects.get(event_type="thread_backfill").payload["cursor"], rows[-1].id)

    def test_anonymized_dialogue_quality_cases(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                # Each case has a separate source, avoiding accidental cross-case context.
                self.cfg = WhatsAppConfig.objects.create(
                    team=self.team, group_jid=case["name"] + "@g.us"
                )
                rows = self.messages(case["messages"])
                result = fixture_result(case, rows)
                self.run_result(rows[-1], result)
                candidates = FactCandidate.objects.filter(
                    trace__raw_message=rows[-1], status="pending"
                )
                self.assertEqual(candidates.count(), case["expected_facts"])
                self.assertEqual(
                    ThreadMessage.objects.filter(raw_message__in=rows)
                    .values("raw_message_id")
                    .distinct()
                    .count(),
                    len(rows),
                )
                for candidate in candidates:
                    ids = set(
                        candidate.thread_revision.thread.message_links.values_list(
                            "raw_message_id", flat=True
                        )
                    )
                    self.assertTrue(
                        set(
                            candidate.evidence.values_list("raw_message_id", flat=True)
                        ).issubset(ids)
                    )
                    self.assertEqual(candidate.thread_revision.state, "ready")

    def test_open_thread_fact_is_published_with_in_progress_flag(self):
        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["state"] = "open"
        self.run_result(rows[-1], result)
        self.assertTrue(FactCandidate.objects.exists())
        candidate = FactCandidate.objects.get(status="pending")
        self.assertTrue(candidate.proposed_changes.get("in_progress"))
        self.assertEqual(self.api.get("/api/threads/").data["count"], 2)
        self.assertEqual(self.api.get("/api/candidates/").data["count"], 1)

    def test_unknown_thread_fact_is_discarded(self):
        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["state"] = "unknown"
        self.run_result(rows[-1], result)
        self.assertFalse(FactCandidate.objects.exists())
        self.assertEqual(self.api.get("/api/threads/").data["count"], 2)
        self.assertEqual(self.api.get("/api/candidates/").data["count"], 0)

    def test_ready_without_final_source_is_rejected(self):
        rows = self.messages(CASES[2]["messages"])
        result = fixture_result(CASES[2], rows)
        for link in result["threads"][0]["messages"]:
            link["thought_state"] = "intermediate"
        with self.assertRaisesMessage(
            ProviderUnavailable, "thread_completion_unproven"
        ):
            self.run_result(rows[-1], result)
        self.assertFalse(FactCandidate.objects.exists())

    def test_reanalysis_preserves_thread_id_and_single_pending_fact(self):
        from .processing_attempts import reserve_attempt

        case = CASES[2]
        rows = self.messages(case["messages"])
        self.run_result(rows[-1], fixture_result(case, rows))
        original = FactCandidate.objects.get(status="pending")
        trace = reserve_attempt(rows[-1], "repeat-test")
        from .dialogue_threads import context_threads

        result = fixture_result(case, rows, context_threads(rows[-1]))
        self.run_result(rows[-1], result, trace.id)
        replacement = FactCandidate.objects.get(status="pending")
        original.refresh_from_db()
        self.assertEqual(original.status, "pending")
        self.assertEqual(original.id, replacement.id)
        self.assertEqual(
            original.thread_revision.thread_id, replacement.thread_revision.thread_id
        )
        self.assertEqual(
            ThreadRevision.objects.filter(
                thread=replacement.thread_revision.thread
            ).count(),
            1,
        )

    def test_foreign_chat_source_and_parent_cycles_are_rejected(self):
        rows = self.messages(CASES[2]["messages"])
        other_cfg = WhatsAppConfig.objects.create(
            team=self.team, group_jid="other@g.us"
        )
        other = RawMessage.objects.create(
            config=other_cfg,
            team=self.team,
            chat_id=other_cfg.group_jid,
            message_id="other",
            content="private",
            timestamp=timezone.now(),
        )
        result = fixture_result(CASES[2], rows)
        result["threads"][0]["messages"][0]["raw_message_id"] = other.id
        with self.assertRaisesMessage(
            ProviderUnavailable, "thread_sources_unavailable"
        ):
            self.run_result(rows[-1], result)
        result = fixture_result(CASES[2], rows)
        result["threads"][0]["parent_key"] = "south"
        result["threads"][1]["parent_key"] = "north"
        with self.assertRaisesMessage(ProviderUnavailable, "thread_parent_invalid"):
            self.run_result(rows[-1], result)

    def test_cross_theme_evidence_and_fabricated_quote_rejected(self):
        rows = self.messages(CASES[2]["messages"])
        result = fixture_result(CASES[2], rows)
        result["facts"][0]["evidence_messages"][0]["raw_message_id"] = rows[1].id
        with self.assertRaisesMessage(
            ProviderUnavailable, "fact_thread_evidence_conflict"
        ):
            self.run_result(rows[-1], result)
        result = fixture_result(CASES[2], rows)
        result["facts"][0]["evidence"] = "fabricated promise"
        with self.assertRaisesMessage(ProviderUnavailable, "evidence_not_in_source"):
            self.run_result(rows[-1], result)

    def test_missing_thread_classification_fails_closed(self):
        rows = self.messages(CASES[2]["messages"])
        result = fixture_result(CASES[2], rows)
        del result["threads"]
        with self.assertRaisesMessage(
            ProviderUnavailable, "thread_classification_missing"
        ):
            self.run_result(rows[-1], result)

    def test_numeric_source_contract(self):
        rows = self.messages(CASES[2]["messages"])
        result = fixture_result(CASES[2], rows)
        result["threads"][0]["messages"][0]["raw_message_id"] = str(rows[0].id)
        with self.assertRaisesMessage(
            ProviderUnavailable, "thread_classification_invalid"
        ):
            self.run_result(rows[-1], result)

    def test_unknown_thread_not_visible_to_other_team(self):
        row = self.messages(["Тогда завтра"])[0]
        self.run_result(row, {"facts": []})
        other = User.objects.create_user("other")
        other_team = Team.objects.create(name="Other")
        TeamMembership.objects.create(
            user=other, team=other_team, role="team_lead", status="active"
        )
        self.api.force_authenticate(other)
        self.assertEqual(self.api.get("/api/threads/").data["count"], 0)
        self.assertEqual(
            self.api.get(
                f"/api/threads/{DialogueThread.objects.get().id}/"
            ).status_code,
            404,
        )

    def test_review_on_catalog_project_does_not_approve_or_send_finances(self):
        from .facts import review

        project = Project.objects.create(
            team=self.team,
            name="БЦ Север",
            source="bitrix_crm",
            bitrix_id="101",
            identity_confirmed=True,
        )
        rows = self.messages(CASES[2]["messages"])
        self.run_result(rows[-1], fixture_result(CASES[2], rows))
        candidate = FactCandidate.objects.get(status="pending")
        self.assertEqual(candidate.project_id, project.id)
        review(candidate.id, self.user, "approve", base_version=0)
        project.refresh_from_db()
        self.assertTrue(project.identity_confirmed)
        self.assertFalse(project.is_verified)
        self.assertFalse(project.needs_bitrix_sync)
        self.assertFalse(OutboxEvent.objects.filter(event_type="crm_sync").exists())

    def test_project_identity_can_be_reviewed_without_inventing_contract_amount(self):
        from .facts import review

        row = self.messages(["Новый объект БЦ Восток"])[0]
        result = {
            "threads": [
                {
                    "key": "east",
                    "topic": "БЦ Восток",
                    "state": "ready",
                    "completion_reason": "Назван конкретный объект",
                    "messages": [{"raw_message_id": row.id, "thought_state": "final"}],
                }
            ],
            "facts": [
                {
                    "thread_key": "east",
                    "evidence_message_id": row.id,
                    "fact_type": "project",
                    "object_name": "БЦ Восток",
                    "evidence": row.content,
                }
            ],
        }
        self.run_result(row, result)
        candidate = FactCandidate.objects.get(status="pending")
        candidate.crm_match_state = "not_found"
        candidate.save()
        review(candidate.id, self.user, "approve", base_version=0)
        project = Project.objects.get()
        self.assertTrue(project.identity_confirmed)
        self.assertFalse(project.is_verified)
        self.assertEqual(project.contract_amount, 0)

    def test_crm_error_does_not_allow_automatic_new_project_creation(self):
        from .facts import review
        from rest_framework.exceptions import ValidationError

        row = self.messages(["Новый объект БЦ Восток"])[0]
        result = {
            "threads": [
                {
                    "key": "east",
                    "topic": "БЦ Восток",
                    "state": "ready",
                    "completion_reason": "Назван конкретный объект",
                    "messages": [{"raw_message_id": row.id, "thought_state": "final"}],
                }
            ],
            "facts": [
                {
                    "thread_key": "east",
                    "evidence_message_id": row.id,
                    "fact_type": "project",
                    "object_name": "БЦ Восток",
                    "evidence": row.content,
                }
            ],
        }
        self.run_result(row, result)
        candidate = FactCandidate.objects.get(status="pending")
        candidate.crm_match_state = "error"
        candidate.save()
        with self.assertRaises(ValidationError):
            review(candidate.id, self.user, "approve", base_version=0)
        self.assertFalse(Project.objects.exists())

    def test_context_uses_saved_revision_after_theme_moves(self):
        rows = self.messages(CASES[2]["messages"])
        self.run_result(rows[-1], fixture_result(CASES[2], rows))
        candidate = FactCandidate.objects.get(status="pending")
        thread = candidate.thread_revision.thread
        thread.topic = "Позднее переименование"
        thread.save()
        thread.message_links.filter(raw_message=rows[0]).delete()
        result = self.api.get(f"/api/candidates/{candidate.id}/context/").data["thread"]
        self.assertEqual(result["topic"], "Смета БЦ Север")
        self.assertIn(rows[0].id, [item["id"] for item in result["messages"]])

    def test_late_closing_message_resolves_manager_from_original_promise(self):
        profile = UserProfile.objects.create(
            user=self.user, full_name="Боб", phone="79990000002"
        )
        rows = self.messages(CASES[2]["messages"])
        rows[-1].sender_phone = profile.phone
        rows[-1].save()
        closing = self.messages(["Тогда завтра"])[0]
        result = fixture_result(CASES[2], rows)
        result["threads"][0]["messages"].append(
            {
                "raw_message_id": closing.id,
                "thought_state": "final",
                "relation": "clarifies",
            }
        )
        self.run_result(closing, result)
        self.assertEqual(
            FactCandidate.objects.get(status="pending").manager_id, profile.id
        )

    def test_unknown_response_cannot_rewrite_approved_obligation(self):
        from .facts import review
        from .processing_attempts import reserve_attempt

        rows = self.messages(CASES[2]["messages"])
        self.run_result(rows[-1], fixture_result(CASES[2], rows))
        candidate = FactCandidate.objects.get(status="pending")
        review(candidate.id, self.user, "approve", base_version=0)
        trace = reserve_attempt(rows[-1], "unknown-reanalysis")
        self.run_result(rows[-1], {"facts": []}, trace.id)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, "approved")
        self.assertEqual(
            candidate.accepted_commitment.commitment_text, "Подготовить смету БЦ Север"
        )

    def test_subthread_can_finish_while_parent_thought_stays_open(self):
        rows = self.messages(CASES[2]["messages"])
        result = fixture_result(CASES[2], rows)
        result["threads"][0]["parent_key"] = "south"
        self.run_result(rows[-1], result)
        candidate = FactCandidate.objects.get(status="pending")
        child = candidate.thread_revision.thread
        self.assertEqual(child.parent.topic, "Доставка БЦ Южный")
        self.assertEqual(child.parent.state, "open")
        self.assertEqual(child.state, "ready")

    def test_stale_generation_requests_fresh_analysis_without_overwriting(self):
        from .processing_attempts import reserve_attempt
        from .dialogue_threads import context_threads

        rows = self.messages(CASES[2]["messages"])
        self.run_result(rows[-1], fixture_result(CASES[2], rows))
        original = FactCandidate.objects.get(status="pending")
        trace = reserve_attempt(rows[-1], "stale-test")
        result = fixture_result(CASES[2], rows, context_threads(rows[-1]))
        endpoint = {
            "tag": "test",
            "api_format": "openai_compatible",
            "effective_provider_url": self.ai.chat_provider_url,
            "context_length": 262144,
        }

        def changed_while_llm_runs(*args, **kwargs):
            from django.db.models import F

            DialogueThread.objects.filter(pk=original.thread_revision.thread_id).update(
                version=F("version") + 1
            )
            return result, {}, {}

        with (
            patch("api.pipeline.AIService._config", return_value=self.ai),
            patch(
                "api.message_context.context_runtime",
                return_value=(Counter(), endpoint),
            ),
            patch(
                "api.pipeline.AIService.analyze_payload",
                side_effect=changed_while_llm_runs,
            ),
        ):
            extract_message(rows[-1].id, trace_id=trace.id)
        original.refresh_from_db()
        self.assertEqual(original.status, "pending")
        self.assertEqual(FactCandidate.objects.filter(status="pending").count(), 1)
        self.assertTrue(
            OutboxEvent.objects.filter(
                deduplication_key=f"thread-conflict:{trace.id}"
            ).exists()
        )

    def test_thread_list_hierarchy_and_annotations(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            team=self.team,
            company=company,
            bitrix_id="8920",
        )
        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["parent_key"] = "south"
        self.run_result(rows[-1], result)

        parent_thread = DialogueThread.objects.get(topic="Доставка БЦ Южный")
        child_thread = DialogueThread.objects.get(topic="Смета БЦ Север")
        self.assertEqual(child_thread.parent, parent_thread)

        parent_thread.project = project
        parent_thread.save()

        resp = self.api.get("/api/threads/")
        self.assertEqual(resp.status_code, 200)
        items = {item["id"]: item for item in resp.data["results"]}

        parent_item = items[parent_thread.id]
        self.assertEqual(parent_item["project_id"], project.id)
        self.assertEqual(parent_item["project_name"], "ЖК Grand Park")
        self.assertEqual(parent_item["company_id"], company.id)
        self.assertEqual(parent_item["company_name"], "ТОО BI Group")
        self.assertEqual(parent_item["children_count"], 1)
        self.assertIsNone(parent_item["parent_id"])

        child_item = items[child_thread.id]
        self.assertEqual(child_item["parent_id"], parent_thread.id)
        self.assertEqual(child_item["children_count"], 0)
        self.assertEqual(child_item["commitments_count"], 1)

    def test_thread_list_filters(self):
        company1 = Company.objects.create(name="Alpha", bitrix_company_id="111")
        company2 = Company.objects.create(name="Beta", bitrix_company_id="222")
        p1 = Project.objects.create(name="Proj A", team=self.team, company=company1, bitrix_id="11")
        p2 = Project.objects.create(name="Proj B", team=self.team, company=company2, bitrix_id="22")

        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["parent_key"] = "south"
        self.run_result(rows[-1], result)

        t_north = DialogueThread.objects.get(topic="Смета БЦ Север")
        t_south = DialogueThread.objects.get(topic="Доставка БЦ Южный")
        t_north.project = p1
        t_north.save()
        t_south.project = p2
        t_south.save()

        # filter by state
        resp_ready = self.api.get("/api/threads/", {"state": "ready"})
        self.assertEqual(resp_ready.data["count"], 1)
        self.assertEqual(resp_ready.data["results"][0]["id"], t_north.id)

        resp_open = self.api.get("/api/threads/", {"state": "open"})
        self.assertEqual(resp_open.data["count"], 1)
        self.assertEqual(resp_open.data["results"][0]["id"], t_south.id)

        resp_all = self.api.get("/api/threads/", {"state": "all"})
        self.assertEqual(resp_all.data["count"], 2)

        # filter by company_id
        resp_c1 = self.api.get("/api/threads/", {"company_id": company1.id})
        self.assertEqual(resp_c1.data["count"], 1)
        self.assertEqual(resp_c1.data["results"][0]["id"], t_north.id)

        # filter by project_id
        resp_p2 = self.api.get("/api/threads/", {"project_id": p2.id})
        self.assertEqual(resp_p2.data["count"], 1)
        self.assertEqual(resp_p2.data["results"][0]["id"], t_south.id)

        # filter by parent_id
        resp_root = self.api.get("/api/threads/", {"parent_id": "null"})
        self.assertEqual(resp_root.data["count"], 1)
        self.assertEqual(resp_root.data["results"][0]["id"], t_south.id)

        resp_child = self.api.get("/api/threads/", {"parent_id": t_south.id})
        self.assertEqual(resp_child.data["count"], 1)
        self.assertEqual(resp_child.data["results"][0]["id"], t_north.id)

        # search
        resp_search = self.api.get("/api/threads/", {"search": "Север"})
        self.assertEqual(resp_search.data["count"], 1)
        self.assertEqual(resp_search.data["results"][0]["id"], t_north.id)

    def test_thread_detail_children_and_facts(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            team=self.team,
            company=company,
            bitrix_id="8920",
        )
        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["parent_key"] = "south"
        self.run_result(rows[-1], result)

        parent_thread = DialogueThread.objects.get(topic="Доставка БЦ Южный")
        child_thread = DialogueThread.objects.get(topic="Смета БЦ Север")
        parent_thread.project = project
        parent_thread.save()

        # Check detail of parent thread
        resp = self.api.get(f"/api/threads/{parent_thread.id}/")
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertEqual(data["project_name"], "ЖК Grand Park")
        self.assertEqual(data["company_name"], "ТОО BI Group")
        self.assertEqual(len(data["children"]), 1)
        self.assertEqual(data["children"][0]["id"], child_thread.id)
        self.assertEqual(data["children"][0]["topic"], "Смета БЦ Север")

        # Check detail of child thread
        resp_child = self.api.get(f"/api/threads/{child_thread.id}/")
        self.assertEqual(resp_child.status_code, 200)
        self.assertEqual(len(resp_child.data["facts"]), 1)
        fact = resp_child.data["facts"][0]
        self.assertEqual(fact["fact_type"], "commitment")
        self.assertEqual(fact["status"], "pending")
        self.assertIn("commitment_text", fact["proposed_changes"])

    def test_thread_list_hierarchy_and_filters(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            team=self.team,
            company=company,
            bitrix_id="8920",
        )
        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["parent_key"] = "south"
        self.run_result(rows[-1], result)

        root = DialogueThread.objects.get(topic="Доставка БЦ Южный")
        child = DialogueThread.objects.get(topic="Смета БЦ Север")
        self.assertEqual(child.parent, root)
        root.project = project
        root.save()

        # Query /api/threads/ and assert that items contain parent_id, project_name, company_name, children_count
        resp = self.api.get("/api/threads/")
        self.assertEqual(resp.status_code, 200)
        items = {item["id"]: item for item in resp.data["results"]}

        root_item = items[root.id]
        self.assertEqual(root_item["project_name"], "ЖК Grand Park")
        self.assertEqual(root_item["company_name"], "ТОО BI Group")
        self.assertEqual(root_item["children_count"], 1)
        self.assertIsNone(root_item["parent_id"])

        child_item = items[child.id]
        self.assertEqual(child_item["parent_id"], root.id)
        self.assertEqual(child_item["children_count"], 0)

        # Test filtering by state, company_id, project_id
        resp_state = self.api.get("/api/threads/", {"state": "open"})
        self.assertEqual(resp_state.status_code, 200)
        self.assertEqual(resp_state.data["count"], 1)
        self.assertEqual(resp_state.data["results"][0]["id"], root.id)

        resp_comp = self.api.get("/api/threads/", {"company_id": company.id})
        self.assertEqual(resp_comp.status_code, 200)
        self.assertEqual(resp_comp.data["count"], 1)
        self.assertEqual(resp_comp.data["results"][0]["id"], root.id)

        resp_proj = self.api.get("/api/threads/", {"project_id": project.id})
        self.assertEqual(resp_proj.status_code, 200)
        self.assertEqual(resp_proj.data["count"], 1)
        self.assertEqual(resp_proj.data["results"][0]["id"], root.id)

        # Query /api/threads/<root_id>/ and assert children list contains the child thread, and company/project details are present
        resp_detail = self.api.get(f"/api/threads/{root.id}/")
        self.assertEqual(resp_detail.status_code, 200)
        detail_data = resp_detail.data
        self.assertEqual(detail_data["project_name"], "ЖК Grand Park")
        self.assertEqual(detail_data["company_name"], "ТОО BI Group")
        self.assertEqual(detail_data["company_id"], company.id)
        self.assertEqual(detail_data["project_id"], project.id)
        self.assertEqual(len(detail_data["children"]), 1)
        self.assertEqual(detail_data["children"][0]["id"], child.id)
        self.assertEqual(detail_data["children"][0]["topic"], "Смета БЦ Север")

    def test_open_thread_facts_retained(self):
        from .dialogue_threads import ready_facts

        themes = {
            "theme_open": {
                "state": "open",
                "messages": [{"raw_message_id": 10}],
            }
        }
        result = {
            "facts": [
                {
                    "thread_key": "theme_open",
                    "evidence_message_id": 10,
                    "fact_type": "project",
                    "evidence": "Объект открытый",
                }
            ]
        }
        filtered = ready_facts(result, themes)
        self.assertEqual(len(filtered["facts"]), 1)
        self.assertTrue(filtered["facts"][0].get("in_progress"))

    def test_thread_subscription_and_toggle(self):
        case = CASES[0]
        rows = self.messages(case["messages"])
        self.run_result(rows[-1], fixture_result(case, rows))
        thread = DialogueThread.objects.first()

        # Initially not subscribed
        resp = self.api.get(f"/api/threads/{thread.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data.get("is_subscribed"))

        # Subscribe
        sub_resp = self.api.post(f"/api/threads/{thread.id}/subscribe/")
        self.assertEqual(sub_resp.status_code, 200)
        self.assertTrue(sub_resp.data.get("subscribed"))
        self.assertEqual(sub_resp.data.get("thread_id"), thread.id)
        self.assertTrue(ThreadSubscription.objects.filter(user=self.user, thread=thread).exists())

        # Check detail reflects subscription
        resp = self.api.get(f"/api/threads/{thread.id}/")
        self.assertTrue(resp.data.get("is_subscribed"))

        # Check list reflects subscription
        list_resp = self.api.get("/api/threads/")
        item = next(it for it in list_resp.data["results"] if it["id"] == thread.id)
        self.assertTrue(item.get("is_subscribed"))

        # Unsubscribe
        unsub_resp = self.api.post(f"/api/threads/{thread.id}/subscribe/")
        self.assertEqual(unsub_resp.status_code, 200)
        self.assertFalse(unsub_resp.data.get("subscribed"))
        self.assertFalse(ThreadSubscription.objects.filter(user=self.user, thread=thread).exists())

        # Check detail reflects unsubscription
        resp = self.api.get(f"/api/threads/{thread.id}/")
        self.assertFalse(resp.data.get("is_subscribed"))

    def test_thread_list_stats_and_priority_ordering(self):
        case = CASES[2]
        rows = self.messages(case["messages"])
        self.run_result(rows[-1], fixture_result(case, rows))

        # Check /api/threads/ pagination and stats
        resp = self.api.get("/api/threads/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("stats", resp.data)
        stats = resp.data["stats"]
        self.assertIn("total", stats)
        self.assertIn("open", stats)
        self.assertIn("ready", stats)
        self.assertIn("commitments", stats)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["open"], 1)
        self.assertEqual(stats["ready"], 1)
        self.assertEqual(stats["commitments"], 1)

        # Check that when filtering by state="open", stats still retains global numbers
        resp_open = self.api.get("/api/threads/", {"state": "open"})
        self.assertEqual(resp_open.status_code, 200)
        self.assertEqual(len(resp_open.data["results"]), 1)
        self.assertEqual(resp_open.data["results"][0]["state"], "open")
        self.assertEqual(resp_open.data["stats"]["total"], 2)
        self.assertEqual(resp_open.data["stats"]["open"], 1)
        self.assertEqual(resp_open.data["stats"]["ready"], 1)
        self.assertEqual(resp_open.data["stats"]["commitments"], 1)

        # Priority ordering: ready thread comes before open thread
        results = resp.data["results"]
        self.assertEqual(results[0]["state"], "ready")

    def test_thread_status_change_notifies_subscribers(self):
        from .processing_attempts import reserve_attempt
        from .dialogue_threads import context_threads

        case = CASES[0]
        rows = self.messages(case["messages"])
        res1 = fixture_result(case, rows)
        self.run_result(rows[-1], res1)
        thread = DialogueThread.objects.get()
        self.assertEqual(thread.state, "open")

        # Subscribe user to thread
        ThreadSubscription.objects.create(user=self.user, thread=thread)

        # Second run completes the theme (state: open -> ready)
        trace = reserve_attempt(rows[-1], "thread-status-test")
        res2 = fixture_result(case, rows, context_threads(rows[-1]))
        res2["threads"][0]["state"] = "ready"
        res2["threads"][0]["completion_reason"] = "договоренность достигнута"
        res2["threads"][0]["messages"][-1]["thought_state"] = "final"
        res2["threads"][0]["expected_version"] = thread.version
        self.run_result(rows[-1], res2, trace.id)

        thread.refresh_from_db()
        self.assertEqual(thread.state, "ready")

        # Verify notification was sent
        notif = Notification.objects.filter(recipient=self.user, category="thread_status").first()
        self.assertIsNotNone(notif)
        self.assertIn("Изменен статус темы", notif.title)
        self.assertIn("В процессе ➔ Завершена", notif.message)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class CatalogTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="CRM")
        self.cfg = BitrixSettings.objects.create(
            webhook_url="https://crm.test/rest/1/test/",
            is_active=True,
            auto_import_deals=True,
        )
        self.lead = User.objects.create_user("lead")
        TeamMembership.objects.create(
            user=self.lead, team=self.team, role="team_lead", status="active"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.lead)
        self.mapping = override_settings(BITRIX_TEAM_ID=self.team.id)
        self.mapping.enable()
        self.addCleanup(self.mapping.disable)

    def page(self, response):
        state = CrmCatalogSync.objects.get(team=self.team)
        payload = {
            "team_id": self.team.id,
            "generation": state.generation,
            "cursor": state.cursor,
        }
        with patch("api.crm_catalog.BitrixService.read_call", return_value=response):
            sync_page(payload)
        return payload

    def test_initial_directory_enqueues_without_external_http(self):
        with patch("api.crm_catalog.BitrixService.read_call") as call:
            self.assertEqual(self.api.get("/api/directory/").status_code, 200)
            self.assertEqual(self.api.get("/api/directory/").status_code, 200)
            call.assert_not_called()
        self.assertEqual(
            OutboxEvent.objects.filter(event_type="crm_catalog").count(), 1
        )

    def test_paginated_idempotent_import_does_not_approve_finances(self):
        enqueue_catalog(self.team.id)
        payload = self.page(
            {
                "result": [{"ID": "10", "TITLE": "Объект", "OPPORTUNITY": "100000"}],
                "next": 50,
            }
        )
        self.page({"result": [{"ID": "11", "TITLE": "Объект"}]})
        sync_page(payload)
        self.assertEqual(Project.objects.count(), 2)
        self.assertFalse(Project.objects.filter(is_verified=True).exists())
        self.assertFalse(FactCandidate.objects.exists())
        self.assertEqual(self.api.get("/api/directory/").data["projects_count"], 2)
        self.assertEqual(Project.objects.get(bitrix_id="10").contract_amount, 0)
        self.assertEqual(CrmCatalogSync.objects.get().state, "succeeded")

    def test_confirmed_values_and_team_boundary_preserved(self):
        project = Project.objects.create(
            team=self.team,
            bitrix_id="10",
            name="Проверенное имя",
            is_verified=True,
            contract_amount=500,
        )
        enqueue_catalog(self.team.id)
        self.page({"result": [{"ID": "10", "TITLE": "CRM имя", "OPPORTUNITY": "999"}]})
        project.refresh_from_db()
        self.assertEqual(project.name, "Проверенное имя")
        self.assertEqual(project.contract_amount, 500)
        other = Team.objects.create(name="Foreign")
        Project.objects.create(team=other, bitrix_id="11", name="Private")
        enqueue_catalog(self.team.id, refresh=True)
        with self.assertRaisesMessage(
            ProviderUnavailable, "crm_project_scope_conflict"
        ):
            self.page({"result": [{"ID": "11", "TITLE": "Private"}]})
        self.assertEqual(CrmCatalogSync.objects.get().state, "error")

    def test_directory_search_beyond_500_and_permissions(self):
        Project.objects.bulk_create(
            [
                Project(
                    team=self.team,
                    source="bitrix_crm",
                    identity_confirmed=True,
                    bitrix_id=str(i),
                    name=f"Проект {i:04}",
                )
                for i in range(1, 552)
            ]
        )
        self.assertEqual(len(self.api.get("/api/directory/").data["projects"]), 100)
        self.assertEqual(
            self.api.get("/api/directory/", {"project_search": "0551"}).data[
                "projects"
            ][0]["name"],
            "Проект 0551",
        )
        manager = User.objects.create_user("manager")
        profile = UserProfile.objects.create(
            user=manager, bitrix_user_id="7", full_name="Manager"
        )
        TeamMembership.objects.create(
            user=manager, team=self.team, role="manager", status="active"
        )
        Project.objects.filter(bitrix_id="551").update(manager=profile)
        self.api.force_authenticate(manager)
        self.assertEqual(self.api.get("/api/directory/").data["projects_count"], 1)
        self.assertEqual(
            self.api.post(
                "/api/directory/crm-sync/", {"team_id": self.team.id}, format="json"
            ).status_code,
            403,
        )

    def test_invalid_pagination_does_not_mark_complete(self):
        enqueue_catalog(self.team.id)
        with self.assertRaisesMessage(ProviderUnavailable, "crm_invalid_pagination"):
            self.page({"result": [{"ID": "10", "TITLE": "Project"}], "next": 0})
        self.assertEqual(CrmCatalogSync.objects.get().state, "error")
        self.assertFalse(Project.objects.exists())

    def test_import_assigns_only_mapped_active_manager(self):
        user = User.objects.create_user("mapped")
        profile = UserProfile.objects.create(
            user=user, full_name="Mapped", bitrix_user_id="7"
        )
        TeamMembership.objects.create(
            user=user, team=self.team, role="manager", status="active"
        )
        enqueue_catalog(self.team.id)
        self.page(
            {
                "result": [
                    {"ID": "10", "TITLE": "Mapped", "ASSIGNED_BY_ID": "7"},
                    {"ID": "11", "TITLE": "Unmapped", "ASSIGNED_BY_ID": "8"},
                ]
            }
        )
        self.assertEqual(Project.objects.get(bitrix_id="10").manager, profile)
        self.assertIsNone(Project.objects.get(bitrix_id="11").manager)

    def test_company_sync_and_deal_company_association(self):
        enqueue_catalog(self.team.id, sync_companies=True)
        state = CrmCatalogSync.objects.get(team=self.team)
        comp_payload = {
            "team_id": self.team.id,
            "generation": state.generation,
            "cursor": state.cursor,
            "phase": "companies",
        }
        with patch(
            "api.crm_catalog.BitrixService.read_call",
            return_value={
                "result": [
                    {"ID": "101", "TITLE": "ТОО Байтерек", "PHONE": [{"VALUE": "+77011112233"}]},
                    {"ID": "102", "TITLE": "ТОО Астана Моторс", "PHONE": []},
                ],
                "next": None,
            },
        ):
            sync_page(comp_payload)

        self.assertEqual(Company.objects.count(), 2)
        c1 = Company.objects.get(bitrix_company_id="101")
        self.assertEqual(c1.name, "ТОО Байтерек")
        self.assertEqual(c1.phone, "+77011112233")

        deal_payload = {
            "team_id": self.team.id,
            "generation": state.generation,
            "cursor": 0,
            "phase": "deals",
        }
        with patch(
            "api.crm_catalog.BitrixService.read_call",
            return_value={
                "result": [
                    {"ID": "201", "TITLE": "Строительство ЖК", "COMPANY_ID": "101", "COMPANY_TITLE": "ТОО Байтерек"},
                    {"ID": "202", "TITLE": "Поставка авто", "COMPANY_ID": "102"},
                ],
                "next": None,
            },
        ):
            sync_page(deal_payload)

        p1 = Project.objects.get(bitrix_id="201")
        self.assertEqual(p1.company, c1)
        self.assertEqual(p1.name, "Строительство ЖК")

        p2 = Project.objects.get(bitrix_id="202")
        self.assertEqual(p2.company.bitrix_company_id, "102")

        resp = self.api.get("/api/directory/")
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertIn("companies", data)
        self.assertEqual(len(data["companies"]), 2)
        proj_dict = {p["id"]: p for p in data["projects"]}
        self.assertEqual(proj_dict[p1.id]["company_name"], "ТОО Байтерек")
        self.assertEqual(proj_dict[p1.id]["company_id"], c1.id)

        search_resp = self.api.get("/api/directory/", {"project_search": "Байтерек"})
        self.assertEqual(len(search_resp.data["projects"]), 1)
        self.assertEqual(search_resp.data["projects"][0]["id"], p1.id)

    def test_scoped_projects_includes_identity_confirmed_crm_deals(self):
        from .datamart import scoped_projects
        p = Project.objects.create(
            team=self.team,
            bitrix_id="999",
            name="CRM Объект",
            source="bitrix_crm",
            identity_confirmed=True,
            is_verified=False,
            version=0,
            currency="KZT",
        )
        qs, currency = scoped_projects(self.lead)
        self.assertIn(p, qs)

        resp = self.api.get("/api/projects/")
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get("results", [])
        self.assertTrue(any(row["id"] == p.id for row in results))
