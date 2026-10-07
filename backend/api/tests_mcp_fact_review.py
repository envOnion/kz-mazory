"""Tests for Fact Review Streamable HTTP MCP Server and Agent LLM Tools."""

import json
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .authentication import create_session, generate_mcp_token
from .models import (
    UserProfile,
    Team,
    TeamMembership,
    Company,
    Project,
    FactCandidate,
    RawMessage,
    MessageProcessingTrace,
    DialogueThread,
    ThreadRevision,
    AuditEvent,
    OutboxEvent,
    ThreadMessage,
    FactEvidence,
    CandidateCrmMatch,
    Notification,
    McpToken,
    WhatsAppConfig,
)


class FactReviewMcpServerTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="79991112233",
            password="secret-password",
        )
        self.profile = UserProfile.objects.create(
            user=self.user,
            full_name="Тестовый Руководитель",
            phone="79991112233",
            role="team_lead",
        )
        self.team = Team.objects.create(name="Команда Продаж")
        TeamMembership.objects.create(
            user=self.user,
            team=self.team,
            role="team_lead",
            status="active",
        )
        self.company = Company.objects.create(name="ТОО ТестСтрой")
        self.project = Project.objects.create(
            name="ЖК Алтын",
            team=self.team,
            company=self.company,
            version=1,
            identity_confirmed=True,
        )

        self.config = WhatsAppConfig.objects.create(
            team=self.team,
            name="Тестовый чат",
            group_jid="test_group@g.us",
        )

        # Create RawMessage and Trace
        self.raw_message = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            project=self.project,
            message_id="mcp_test_msg_1",
            sender_phone="77771234567",
            sender_name="Асет",
            content="Согласовали оплату 25 000 000 ₸ по ЖК Алтын до 25 октября",
            timestamp=timezone.now(),
        )
        self.trace = MessageProcessingTrace.objects.create(
            raw_message=self.raw_message,
            whatsapp_message_id=self.raw_message.message_id,
            whatsapp_content=self.raw_message.content,
            pipeline_action="commitment_created",
            ai_confidence=0.96,
            result_summary="Клиент пообещал совершить оплату 25 млн ₸ до конца месяца",
        )

        # Create DialogueThread and ThreadMessage
        self.thread = DialogueThread.objects.create(
            team=self.team,
            project=self.project,
            topic="Оплата ЖК Алтын",
            state="open",
        )
        self.thread_msg = ThreadMessage.objects.create(
            thread=self.thread,
            raw_message=self.raw_message,
            thought_state="final",
            relation="discusses",
        )

        # Create FactCandidate
        self.candidate = FactCandidate.objects.create(
            team=self.team,
            project=self.project,
            manager=self.profile,
            trace=self.trace,
            fact_type="commitment",
            status="pending",
            source_key="mcp_test_cand_1",
            confidence=0.96,
            base_project_version=1,
            proposed_changes={
                "fact_type": "commitment",
                "evidence": "оплату 25 000 000 ₸ по ЖК Алтын до 25 октября",
                "object_name": "ЖК Алтын",
                "amount": "25000000.00",
                "currency": "KZT",
                "deadline_at": "2026-10-25T18:00:00Z",
                "commitment_text": "Оплата 25 000 000 ₸ до 25 октября",
            },
        )
        FactEvidence.objects.create(
            candidate=self.candidate,
            raw_message_id=self.raw_message.id,
            quote="оплату 25 000 000 ₸ по ЖК Алтын до 25 октября",
            field_name="promise",
        )

    def _call_mcp(self, method, params=None, headers=None, client=None):
        c = client or self.client
        if method == "initialize" and not params:
            params = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        hdrs = headers or {}
        hdrs.setdefault("HTTP_ACCEPT", "application/json, text/event-stream")
        return c.post(
            "/api/mcp/fact-review/",
            data=json.dumps(payload),
            content_type="application/json",
            **hdrs,
        )

    def test_unauthenticated_request_rejected(self):
        resp = self._call_mcp("initialize")
        self.assertEqual(resp.status_code, 401)

    def test_authenticated_via_jwt_session(self):
        _, access_token, _ = create_session(self.user)
        resp = self._call_mcp(
            "initialize",
            headers={"HTTP_AUTHORIZATION": f"Bearer {access_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result"]["serverInfo"]["name"], "mazory-fact-review")

    def test_authenticated_via_mcp_token(self):
        token_obj, raw_token = generate_mcp_token(self.user, name="Agent Test")
        resp = self._call_mcp(
            "initialize",
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result"]["serverInfo"]["name"], "mazory-fact-review")

        # Also test with HTTP_X_API_KEY
        resp2 = self._call_mcp(
            "initialize",
            headers={"HTTP_X_API_KEY": raw_token},
        )
        self.assertEqual(resp2.status_code, 200)

    def test_inactive_mcp_token_rejected(self):
        token_obj, raw_token = generate_mcp_token(self.user, name="Inactive Agent")
        token_obj.is_active = False
        token_obj.save()

        resp = self._call_mcp(
            "initialize",
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_tools_list_discovery(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/list",
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        tools = {t["name"] for t in data["result"]["tools"]}
        expected = {
            "list_candidates",
            "get_candidate_details",
            "get_candidate_context",
            "list_projects",
            "review_candidate",
        }
        self.assertTrue(expected.issubset(tools))

    def test_tool_list_candidates(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={"name": "list_candidates", "arguments": {"status": "pending"}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        content = json.loads(data["result"]["content"][0]["text"])
        self.assertGreaterEqual(content["total_count"], 1)
        found = any(c["id"] == self.candidate.id for c in content["candidates"])
        self.assertTrue(found)

    def test_tool_get_candidate_details(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={"name": "get_candidate_details", "arguments": {"candidate_id": self.candidate.id}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        content = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(content["id"], self.candidate.id)
        self.assertEqual(content["fact_type"], "commitment")
        self.assertEqual(len(content["evidence"]), 1)

    def test_tool_get_candidate_details_with_crm_match(self):
        match = CandidateCrmMatch.objects.create(
            candidate=self.candidate,
            project=self.project,
            crm_match_revision=2,
            bitrix_deal_id="718",
            deal_title="ЦТП 343 квартал",
            company_name="Top Build",
            opportunity=Decimal("117000000.00"),
            stage_id="EXECUTING",
            selection_state="selected",
        )
        self.candidate.crm_match_state = "matched"
        self.candidate.crm_match_revision = 2
        self.candidate.save(update_fields=["crm_match_state", "crm_match_revision"])
        _, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={"name": "get_candidate_details", "arguments": {"candidate_id": self.candidate.id}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertFalse(result.get("isError", False), result)
        content = json.loads(result["content"][0]["text"])
        self.assertEqual(content["crm_match_revision"], 2)
        self.assertEqual(content["crm_match_state"], "matched")
        self.assertEqual(content["base_version"], 1)
        self.assertEqual(content["crm_matches"], [{
            "id": match.id,
            "bitrix_deal_id": "718",
            "title": "ЦТП 343 квартал",
            "company_title": "Top Build",
            "opportunity": "117000000.00",
            "stage_id": "EXECUTING",
            "selection_state": "selected",
        }])

    def test_tool_get_candidate_context(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={"name": "get_candidate_context", "arguments": {"candidate_id": self.candidate.id}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        content = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(content["candidate_id"], self.candidate.id)
        self.assertIn("клиент пообещал", content["trace"]["thought_trace"].lower())
        self.assertIn("25 000 000", content["source_message"]["content"])
        self.assertEqual(len(content["thread_messages"]), 1)

    def test_tool_list_projects(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={"name": "list_projects", "arguments": {"search": "Алтын"}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        content = json.loads(data["result"]["content"][0]["text"])
        self.assertGreaterEqual(content["total_count"], 1)
        self.assertEqual(content["projects"][0]["name"], "ЖК Алтын")

    def test_tool_review_candidate_approve(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={
                "name": "review_candidate",
                "arguments": {
                    "candidate_id": self.candidate.id,
                    "action": "approve",
                    "reason": "Подтверждено агентской LLM",
                },
            },
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        content = json.loads(data["result"]["content"][0]["text"])
        self.assertTrue(content["success"])
        self.assertEqual(content["status"], "approved")

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, "approved")

    def test_tool_review_candidate_reject(self):
        token_obj, raw_token = generate_mcp_token(self.user)
        resp = self._call_mcp(
            "tools/call",
            params={
                "name": "review_candidate",
                "arguments": {
                    "candidate_id": self.candidate.id,
                    "action": "reject",
                    "reason": "Сумма не согласована клиентом",
                },
            },
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        content = json.loads(data["result"]["content"][0]["text"])
        self.assertTrue(content["success"])
        self.assertEqual(content["status"], "rejected")

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, "rejected")

    def test_mcp_token_management_api(self):
        _, access_token, _ = create_session(self.user)
        auth_header = {"HTTP_AUTHORIZATION": f"Bearer {access_token}"}

        # 1. Create Token
        resp = self.client.post(
            "/api/mcp/tokens/",
            data={"name": "Agent Alpha", "expires_in_days": 30},
            **auth_header,
        )
        self.assertEqual(resp.status_code, 201)
        created = resp.json()
        self.assertTrue(created["token"].startswith("mcp_"))
        token_id = created["id"]

        # 2. List Tokens
        resp_list = self.client.get("/api/mcp/tokens/", **auth_header)
        self.assertEqual(resp_list.status_code, 200)
        tokens = resp_list.json()["tokens"]
        self.assertTrue(any(t["id"] == token_id for t in tokens))

        # 3. Revoke Token
        resp_del = self.client.delete(
            "/api/mcp/tokens/",
            data={"token_id": token_id},
            content_type="application/json",
            **auth_header,
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertEqual(resp_del.json()["status"], "revoked")


    def _review_tool(self, **arguments):
        _, token = generate_mcp_token(self.user)
        response = self._call_mcp("tools/call", {
            "name": "review_candidate",
            "arguments": {"candidate_id": self.candidate.id, **arguments},
        }, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        return json.loads(response.json()["result"]["content"][0]["text"])

    def _project_candidate(self, **changes):
        self.candidate.fact_type = "project"
        self.candidate.proposed_changes = {
            "fact_type": "project", "object_name": self.project.name,
            "evidence": self.raw_message.content, **changes,
        }
        self.candidate.save()

    def test_match_clears_incompatible_crm_selection_then_allows_approval(self):
        self._project_candidate()
        self.project.bitrix_id = "old"
        self.project.save()
        self.candidate.crm_match_state = "matched"
        self.candidate.crm_match_revision = 1
        self.candidate.save()
        option = CandidateCrmMatch.objects.create(
            candidate=self.candidate, project=self.project, crm_match_revision=1,
            bitrix_deal_id="old", selection_state="selected",
        )
        correct = Project.objects.create(team=self.team, name="Шугла", bitrix_id="correct", version=3)
        result = self._review_tool(action="match", project_id=correct.id, base_version=3,
            reason="Первичный отчёт относится к Шугле, а не другому объекту компании.")
        self.assertTrue(result["success"])
        option.refresh_from_db()
        self.assertEqual(option.selection_state, "dismissed")
        result = self._review_tool(action="approve", base_version=3,
            changes={"object_name": "Шугла", "current_action": "Подписаны накладные"}, reason="Объект проверен по переписке.")
        self.assertTrue(result["success"])
        correct.refresh_from_db()
        self.assertEqual(correct.current_action, "Подписаны накладные")
        self.project.refresh_from_db()
        self.assertEqual(self.project.current_action, "")

    def test_edit_removes_unknown_cost_without_confirming_project(self):
        TeamMembership.objects.filter(user=self.user, team=self.team).update(role="finance")
        self._project_candidate(contract_amount="25000000.00", cost_amount="0.00")
        result = self._review_tool(action="edit", remove_fields=["cost_amount"], base_version=1,
            reason="Источник подтверждает договор, но не нулевую себестоимость.")
        self.assertTrue(result["success"])
        self.candidate.refresh_from_db()
        self.assertNotIn("cost_amount", self.candidate.proposed_changes)
        self.assertEqual(self.candidate.status, "pending")
        self.assertIn("нулевую себестоимость", self.candidate.review_reason)
        self.project.refresh_from_db()
        self.assertFalse(self.project.cost_confirmed)
        self.assertEqual(self.project.contract_amount, 0)
        self.assertFalse(OutboxEvent.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(target_id=self.candidate.id, action="edit", actor=self.user).exists())

    def test_edit_financial_fields_requires_finance_even_when_adding_new_amount(self):
        self._project_candidate()
        result = self._review_tool(action="edit", changes={"contract_amount": "25000000.00"},
            base_version=1, reason="Указана сумма договора.")
        self.assertIn("error", result)
        self.candidate.refresh_from_db()
        self.assertNotIn("contract_amount", self.candidate.proposed_changes)

    def test_edit_rejects_stale_version_and_cannot_delete_evidence(self):
        result = self._review_tool(action="edit", changes={"responsible_name": "Асет"},
            base_version=0, reason="Уточнение ответственного.")
        self.assertIn("error", result)
        result = self._review_tool(action="edit", remove_fields=["evidence"],
            base_version=1, reason="Удаление доказательства запрещено.")
        self.assertIn("error", result)
        self.candidate.refresh_from_db()
        self.assertTrue(self.candidate.proposed_changes["evidence"])

    def test_unmatch_dismisses_completed_search_but_keeps_search_errors(self):
        self._project_candidate()
        self.candidate.crm_match_state = "matched"
        self.candidate.crm_match_revision = 1
        self.candidate.save()
        option = CandidateCrmMatch.objects.create(candidate=self.candidate,
            crm_match_revision=1, bitrix_deal_id="wrong", selection_state="selected")
        result = self._review_tool(action="unmatch", base_version=1,
            reason="Сделка содержит другой объект, все варианты просмотрены.")
        self.assertTrue(result["success"])
        self.candidate.refresh_from_db(); option.refresh_from_db()
        self.assertIsNone(self.candidate.project_id)
        self.assertEqual(self.candidate.crm_match_state, "not_found")
        self.assertEqual(option.selection_state, "dismissed")
        self.candidate.crm_match_revision = 2
        self.candidate.crm_match_state = "error"
        self.candidate.crm_match_error_code = "crm_unavailable"
        self.candidate.save()
        result = self._review_tool(action="unmatch", base_version=0, reason="Снята локальная связь.")
        self.assertTrue(result["success"])
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.crm_match_state, "error")
        self.assertEqual(self.candidate.crm_match_error_code, "crm_unavailable")

    def test_two_projects_in_one_report_are_approved_independently(self):
        self._project_candidate(object_name="БЦ Восток", company_name=self.company.name)
        revision = ThreadRevision.objects.create(thread=self.thread, version=1, state="ready")
        self.candidate.thread_revision = revision
        self.candidate.project = None
        self.candidate.base_project_version = 0
        self.candidate.crm_match_state = "not_found"
        self.candidate.save()
        other = FactCandidate.objects.create(team=self.team, trace=self.trace, source_key="other-project",
            thread_revision=revision, fact_type="project", crm_match_state="not_found",
            proposed_changes={"fact_type": "project", "object_name": "БЦ Запад", "company_name": "Заказчик Запад", "evidence": self.raw_message.content,
                "next_action": "Просчитать другой объект"})
        result = self._review_tool(action="approve", base_version=0, reason="Подтверждён первый объект.")
        self.assertTrue(result["success"])
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.project.company_id, self.company.id)
        other.refresh_from_db(); self.thread.refresh_from_db()
        self.assertIsNone(other.project_id)
        self.assertIsNone(self.thread.project_id)
        self.candidate = other
        result = self._review_tool(action="approve", base_version=0, reason="Подтверждён отдельный второй объект.")
        self.assertTrue(result["success"])
        other.refresh_from_db()
        self.assertEqual(other.project.name, "БЦ Запад")
        self.assertEqual(other.project.company.name, "Заказчик Запад")
        self.assertEqual(other.project.company.team_id, self.team.id)
        self.assertEqual(Project.objects.filter(name__in=["БЦ Восток", "БЦ Запад"]).count(), 2)


    def test_edit_can_add_accessible_fulfillment_after_original_ai_snapshot(self):
        self.trace.context_metadata = {"snapshot_max_id": self.raw_message.id}
        self.trace.save()
        self.candidate.proposed_changes.update({"promise_message_id": self.raw_message.id,
            "responsible_name": "Асет", "assignment_kind": "reported_promise"})
        self.candidate.save()
        done = RawMessage.objects.create(config=self.config, team=self.team, message_id="reviewed-fulfillment",
            timestamp=self.raw_message.timestamp + timedelta(minutes=1), content="Оплата выполнена, деньги поступили.")
        refs = [{"raw_message_id": self.raw_message.id, "quote": self.candidate.proposed_changes["evidence"], "role": "promise"},
            {"raw_message_id": done.id, "quote": done.content, "role": "fulfillment"}]
        result = self._review_tool(action="edit", base_version=1, reason="Просмотрено подтверждение после исходного обещания.",
            changes={"evidence_messages": refs, "fulfillment_message_id": done.id, "commitment_status": "fulfilled"})
        self.assertTrue(result["success"])
        result = self._review_tool(action="approve", base_version=1, reason="Выполнение явно подтверждено последующей репликой.")
        self.assertTrue(result["success"])
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.proposed_changes["commitment_status"], "fulfilled")
        self.assertTrue(self.candidate.proposed_changes["fulfilled_at"])
        self.trace.refresh_from_db()
        self.assertEqual(self.trace.context_metadata["snapshot_max_id"], self.raw_message.id)

    def test_edit_evidence_rejects_invented_quotes_and_other_chat(self):
        result = self._review_tool(action="edit", base_version=1, reason="Несуществующая цитата запрещена.",
            changes={"evidence_messages": [{"raw_message_id": self.raw_message.id,
                "quote": "Все работы полностью закончены", "role": "fulfillment"}]})
        self.assertIn("error", result)
        other = WhatsAppConfig.objects.create(team=self.team, name="Другой чат", group_jid="other@g.us")
        message = RawMessage.objects.create(config=other, team=self.team, message_id="other-chat", timestamp=timezone.now(), content="Оплата выполнена")
        result = self._review_tool(action="edit", base_version=1, reason="Другой чат не доказывает обещание исходного чата.",
            changes={"evidence_messages": [{"raw_message_id": message.id, "quote": message.content, "role": "fulfillment"}]})
        self.assertIn("error", result)
        self.assertEqual(self.candidate.evidence.count(), 1)
