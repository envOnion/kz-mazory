"""Tests for Fact Review Streamable HTTP MCP Server and Agent LLM Tools."""

import json
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
    ThreadMessage,
    FactEvidence,
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
