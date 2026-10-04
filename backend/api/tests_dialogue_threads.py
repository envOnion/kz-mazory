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

    def test_intermediate_fact_is_not_published_even_if_model_returns_it(self):
        case = CASES[2]
        rows = self.messages(case["messages"])
        result = fixture_result(case, rows)
        result["threads"][0]["state"] = "open"
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


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    PROVIDER_ALLOWED_HOSTS=["crm.test"],
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
