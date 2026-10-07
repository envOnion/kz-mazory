"""Regression coverage for personal MCP access and recoverable review context."""
import json
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from .authentication import create_session, generate_mcp_token, token_hash
from .mcp_tokens import ensure_portal_token, reveal
from .models import (Team, TeamMembership, WhatsAppConfig, RawMessage, MessageProcessingTrace,
                     DialogueThread, ThreadMessage, ThreadRevision, FactCandidate, BitrixSettings,
                     McpToken, OutboxEvent, Project)
from .candidate_context import candidate_context


class ImportReviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("79991234567")
        self.team = Team.objects.create(name="Review")
        TeamMembership.objects.create(user=self.user, team=self.team, role="team_lead", status="active")
        self.config = WhatsAppConfig.objects.create(team=self.team, group_jid="review@g.us")
        self.client = APIClient()
        _, access, _ = create_session(self.user)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + access)

    def raw(self, content="Отчёт по Северу", **kwargs):
        return RawMessage.objects.create(team=self.team, config=self.config, chat_id=self.config.group_jid,
            message_id=f"review:{RawMessage.objects.count()}", timestamp=timezone.now(), content=content, **kwargs)

    def candidate(self, raw=None, **kwargs):
        raw = raw or self.raw()
        trace = MessageProcessingTrace.objects.create(raw_message=raw, whatsapp_message_id=raw.message_id, whatsapp_content=raw.content)
        return FactCandidate.objects.create(team=self.team, trace=trace, fact_type="project", source_key=f"review:{trace.pk}", proposed_changes={"object_name": "Север"}, **kwargs)

    def test_otp_issues_stable_encrypted_token_and_cabinet_can_retrieve_it(self):
        with patch("api.auth_views.otp.verify", return_value=True):
            response = self.client.post("/api/auth/verify-code/", {"phone": self.user.username, "code": "1234"})
            self.assertEqual(response.status_code, 200)
            self.client.post("/api/auth/verify-code/", {"phone": self.user.username, "code": "1234"})
        self.assertEqual(McpToken.objects.filter(purpose="portal", is_active=True).count(), 1)
        response = self.client.get("/api/mcp/connection/")
        token = response.data["connection"]["token"]
        self.assertEqual(response["Cache-Control"], "no-store")
        stored = McpToken.objects.get(purpose="portal")
        self.assertNotIn(token, stored.token_encrypted)
        self.assertEqual(stored.token_hash, token_hash(token))
        self.assertEqual(self.client.get("/api/mcp/connection/").data["connection"]["token"], token)

    def test_rotation_and_revoke_do_not_recreate_on_get(self):
        self.client.post("/api/mcp/connection/", {"action": "create"})
        original = McpToken.objects.get(is_active=True)
        old = reveal(original)
        response = self.client.post("/api/mcp/connection/", {"action": "rotate"})
        self.assertNotEqual(response.data["connection"]["token"], old)
        original.refresh_from_db(); self.assertFalse(original.is_active)
        self.client.post("/api/mcp/connection/", {"action": "revoke"})
        self.assertIsNone(self.client.get("/api/mcp/connection/").data["connection"])
        self.assertEqual(McpToken.objects.filter(is_active=True).count(), 0)

    def test_mcp_credential_cannot_read_secret_or_general_rest(self):
        raw = reveal(ensure_portal_token(self.user))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + raw)
        for path in ("/api/mcp/connection/", "/api/mcp/tokens/", "/api/directory/"):
            self.assertEqual(self.client.get(path).status_code, 401)
        for method in ("initialize", "tools/list"):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method}
            if method == "initialize":
                payload["params"] = {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
            response = self.client.post("/api/mcp/fact-review/", json.dumps(payload), content_type="application/json", HTTP_ACCEPT="application/json, text/event-stream")
            self.assertEqual(response.status_code, 200)
            self.assertIn("result", response.json())

    def test_database_prevents_two_active_portal_tokens(self):
        ensure_portal_token(self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            McpToken.objects.create(user=self.user, purpose="portal", token_hash="duplicate")

    def test_legacy_external_token_remains_valid_and_unchanged(self):
        external, raw = generate_mcp_token(self.user)
        ensure_portal_token(self.user)
        external.refresh_from_db()
        self.assertTrue(external.is_active)
        self.assertEqual(external.purpose, "external")
        self.assertEqual(external.token_hash, token_hash(raw))

    @override_settings(MFA_ENCRYPTION_KEY="invalid-key")
    def test_unavailable_encryption_does_not_break_otp_login(self):
        with patch("api.auth_views.otp.verify", return_value=True):
            response = self.client.post("/api/auth/verify-code/", {"phone": self.user.username, "code": "1234"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(McpToken.objects.count(), 0)
        self.assertEqual(self.client.post("/api/mcp/connection/", {"action": "create"}).status_code, 503)

    def test_other_user_cannot_get_owners_connection(self):
        ensure_portal_token(self.user)
        other = User.objects.create_user("79991234568")
        TeamMembership.objects.create(user=other, team=self.team, role="team_lead", status="active")
        _, access, _ = create_session(other)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + access)
        self.assertIsNone(self.client.get("/api/mcp/connection/").data["connection"])

    def test_context_uses_candidate_revision_and_mixed_transport(self):
        original = self.raw()
        unrelated = self.raw("Юг: другая тема")
        after = self.raw("Принято", source="whatsapp_export")
        c = self.candidate(original)
        wrong = DialogueThread.objects.create(team=self.team, identity="wrong", topic="Юг")
        ThreadMessage.objects.create(thread=wrong, raw_message=original, thought_state="final")
        correct = DialogueThread.objects.create(team=self.team, identity="correct", topic="Север")
        ThreadMessage.objects.create(thread=correct, raw_message=original, thought_state="intermediate")
        ThreadMessage.objects.create(thread=correct, raw_message=after, thought_state="final")
        revision = ThreadRevision.objects.create(thread=correct, version=1, state="ready", topic="Север", message_snapshot=[{"raw_message_id": original.id, "thought_state": "intermediate"}, {"raw_message_id": after.id, "thought_state": "final"}])
        c.thread_revision = revision; c.save()
        context = candidate_context(c, self.user, {"direction": "around", "ai_page": 1})
        self.assertEqual(context["thread"]["id"], correct.id)
        self.assertEqual([row["id"] for row in context["messages"]], [original.id, unrelated.id, after.id])
        c.trace.context_metadata = {"included_messages_count": 4, "history_complete_in_request": True}; c.trace.save()
        self.assertIn("повреждён", candidate_context(c, self.user, {"direction": "around", "ai_page": 1})["coverage"])

    def test_retry_disabled_candidate_is_idempotent_and_does_not_write_crm(self):
        BitrixSettings.objects.create(crm_matching_enabled=True, auto_import_deals=False)
        c = self.candidate(crm_match_state="disabled")
        with patch("api.bitrix_service.BitrixService._request") as request:
            result = self.client.post("/api/candidates/crm-retry/", {"team_id": self.team.pk})
            self.assertEqual(result.status_code, 202)
            self.assertEqual(result.data["queued"], 1)
            self.assertEqual(self.client.post("/api/candidates/crm-retry/", {"team_id": self.team.pk}).data["queued"], 0)
            request.assert_not_called()
        c.refresh_from_db()
        self.assertEqual(c.crm_match_state, "queued")
        self.assertEqual(OutboxEvent.objects.filter(event_type="crm_match").count(), 1)
        self.assertEqual(c.status, "pending")

    def test_retry_does_not_invent_project_for_general_commitment(self):
        BitrixSettings.objects.create(crm_matching_enabled=True)
        c = self.candidate()
        c.fact_type = "commitment"; c.proposed_changes = {}; c.save()
        result = self.client.post("/api/candidates/crm-retry/", {"team_id": self.team.pk})
        self.assertEqual(result.data, {"queued": 0, "skipped": 1})
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_matching_permission_allows_catalog_without_automatic_import(self):
        from .crm_catalog import enqueue_catalog, sync_page
        from .bitrix_service import CrmReadConfig
        BitrixSettings.objects.create(crm_matching_enabled=True, auto_import_deals=False)
        sync = enqueue_catalog(self.team.id)
        event = OutboxEvent.objects.get(event_type="crm_catalog")
        config = CrmReadConfig("https://bitrix.example.test/rest/1/test/", 0, "")
        with override_settings(BITRIX_TEAM_ID=self.team.id), patch("api.crm_catalog.CrmReadConfig.from_model", return_value=config), patch("api.crm_catalog.BitrixService.read_call", return_value={"result": [{"ID": "17", "TITLE": "Север"}]}) as read:
            sync_page(event.payload)
        self.assertEqual(read.call_args.args[0], "crm.deal.list")
        sync.refresh_from_db()
        self.assertEqual((sync.state, sync.imported_count), ("succeeded", 1))
        project = Project.objects.get(bitrix_id="17")
        self.assertTrue(project.identity_confirmed)
        self.assertFalse(project.is_verified)

    def test_company_identified_payment_is_eligible_for_retry(self):
        BitrixSettings.objects.create(crm_matching_enabled=True)
        c = self.candidate()
        c.fact_type = "payment"
        c.proposed_changes = {"company_name": "ТОО Север", "amount": "100000"}
        c.save()
        result = self.client.post("/api/candidates/crm-retry/", {"team_id": self.team.pk})
        self.assertEqual(result.data, {"queued": 1, "skipped": 0})
        self.assertFalse(Project.objects.exists())

    def test_worker_cannot_retry_crm_for_team(self):
        BitrixSettings.objects.create(crm_matching_enabled=True)
        self.candidate(crm_match_state="disabled")
        TeamMembership.objects.filter(user=self.user).update(role="worker")
        self.assertEqual(self.client.post("/api/candidates/crm-retry/", {"team_id": self.team.pk}).status_code, 403)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_expired_and_revoked_credentials_stop_authorizing_mcp(self):
        token = ensure_portal_token(self.user)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + reveal(token))
        McpToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post("/api/mcp/fact-review/", {}).status_code, 401)
        McpToken.objects.filter(pk=token.pk).update(expires_at=None, is_active=False)
        self.assertEqual(self.client.post("/api/mcp/fact-review/", {}).status_code, 401)

    def test_time_neighbors_ignore_import_id_distance(self):
        from .message_context import history_queryset
        oldest = self.raw("Давнее", source="whatsapp_export")
        target = self.raw("Принято")
        imported = self.raw("Отчёт", source="whatsapp_export")
        RawMessage.objects.filter(pk=oldest.pk).update(timestamp=target.timestamp - timedelta(days=30))
        RawMessage.objects.filter(pk=imported.pk).update(timestamp=target.timestamp - timedelta(minutes=1))
        qs, _ = history_queryset(target, imported.pk, True)
        self.assertEqual(list(qs.values_list("id", flat=True)), [imported.id, oldest.id])

    def test_acknowledgement_reuses_topic_without_erasing_earlier_fact(self):
        from .dialogue_threads import prepare_themes, persist_themes, source_key
        original = self.raw("Объект Север: оплатили 100000")
        c = self.candidate(original)
        thread = DialogueThread.objects.create(team=self.team, config=self.config, identity="north", source_key=source_key(original), topic="Оплата Север", version=1)
        ThreadMessage.objects.create(thread=thread, raw_message=original, thought_state="final")
        revision = ThreadRevision.objects.create(thread=thread, version=1, state="ready", topic=thread.topic)
        c.thread_revision = revision; c.save()
        reply = self.raw("Принято")
        trace = MessageProcessingTrace.objects.create(raw_message=reply, whatsapp_message_id=reply.message_id, whatsapp_content=reply.content,
            earlier_messages_context=[{"raw_message_id": original.id}], context_metadata={"snapshot_max_id": reply.id, "batch_message_ids": [reply.id], "known_threads": [{"id": thread.id, "topic": thread.topic, "version": 1, "message_ids": [original.id]}]})
        result = {"threads": [{"key": "north", "topic": thread.topic, "summary": "Отчёт об оплате и ответ получателя", "state": "ready", "completion_reason": "Получатель ответил на отчёт", "messages": [{"raw_message_id": original.id, "thought_state": "final"}, {"raw_message_id": reply.id, "thought_state": "final", "relation": "answers"}]}], "facts": []}
        themes = prepare_themes(result, reply, trace)
        self.assertEqual(themes["north"]["thread_id"], thread.id)
        with transaction.atomic():
            persist_themes(themes, reply, trace, [])
        c.refresh_from_db()
        self.assertEqual(c.status, "pending")
        self.assertEqual(DialogueThread.objects.count(), 1)
        self.assertEqual(thread.message_links.count(), 2)
