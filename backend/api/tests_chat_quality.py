from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from api.testing.factories import setup_case, client_for
from api.models import (
    RawMessage,
    FactCandidate,
    AsyncOperation,
    OutboxEvent,
    ChatAccess,
)
from api.pipeline import extract_message
from api.operations import execute_operation
from api.providers import ProviderUnavailable
from api.ai_service import AIService
from api.qdrant_service import qdrant_service


class ChatQualityAndValidationTests(TestCase):
    def setUp(self):
        setup_case(self)

    @patch("api.ai_service.AIService.analyze_message_with_context")
    def test_financial_contradictions_are_blocked(self, ai):
        corpus = [
            "Alpha не оплатили 100",
            "Alpha оплатим 100",
            "Alpha төленбеді 100",
            "Alpha төлейміз 100",
        ]
        for i, text in enumerate(corpus):
            raw = RawMessage.objects.create(
                config=self.config,
                message_id=f"contradiction-{i}",
                timestamp=timezone.now(),
                content=text,
            )
            ai.return_value = {
                "facts": [{"fact_type": "payment", "amount": "100", "evidence": text}]
            }
            with self.assertRaises(ProviderUnavailable):
                extract_message(raw.id)
        self.assertFalse(FactCandidate.objects.exists())

    @patch("api.ai_service.AIService.analyze_message_with_context")
    def test_cumulative_is_not_increment(self, ai):
        raw = RawMessage.objects.create(
            config=self.config,
            message_id="total",
            timestamp=timezone.now(),
            content="Alpha всего оплачено 100",
        )
        ai.return_value = {
            "facts": [
                {"fact_type": "payment", "amount": "100", "evidence": raw.content}
            ]
        }
        extract_message(raw.id)
        self.assertEqual(
            FactCandidate.objects.get().proposed_changes["payment_kind"], "cumulative"
        )

    @patch("api.ai_service.AIService.analyze_message_with_context")
    def test_evidence_must_be_exact_and_unknown_date_remains_unknown(self, ai):
        raw = RawMessage.objects.create(
            config=self.config,
            message_id="no-date",
            timestamp=timezone.now(),
            content="Сделаем завтра",
            sent_at_known=False,
        )
        ai.return_value = {
            "facts": [
                {
                    "fact_type": "commitment",
                    "commitment_text": "Сделаем",
                    "deadline_at": (timezone.now() + timedelta(days=1)).isoformat(),
                    "evidence": raw.content,
                }
            ]
        }
        extract_message(raw.id)
        fact = FactCandidate.objects.get().proposed_changes
        self.assertIsNone(fact["deadline_at"])
        self.assertEqual(fact["deadline_precision"], "unknown")

    def test_chat_is_async_scoped_and_revocation_invalidates_result(self):
        client = client_for(self.manager)
        response = client.post(
            "/api/chat/query/",
            {"prompt": "Покажи KPI", "idempotency_key": "one"},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        op = AsyncOperation.objects.get(id=response.json()["operation_id"])
        self.assertEqual(op.status, "queued")
        execute_operation(op.id)
        data = client.get(f"/api/operations/{op.id}/").json()
        self.assertEqual(data["status"], "succeeded")
        self.assertNotIn(self.foreign.name, str(data))
        ChatAccess.objects.filter(user=self.manager).update(is_active=False)
        self.assertEqual(
            client.get(f"/api/operations/{op.id}/").json()["status"], "expired"
        )

    @patch(
        "api.qdrant_service.QdrantService.search",
        side_effect=ProviderUnavailable("ai_unavailable"),
    )
    def test_ai_error_is_visible_without_breaking_kpi(self, _):
        client = client_for(self.manager)
        response = client.post(
            "/api/chat/query/",
            {"prompt": "Что сказали вчера?", "idempotency_key": "error"},
            format="json",
        )
        execute_operation(response.json()["operation_id"])
        op = AsyncOperation.objects.get(pk=response.json()["operation_id"])
        self.assertEqual(op.status, "failed")
        self.assertEqual(client.get("/api/kpi/summary/").status_code, 200)

    def test_search_without_allowed_chats_returns_nothing(self):
        with patch("api.qdrant_service.QdrantService.ensure_collection") as ensure:
            self.assertEqual(qdrant_service.search("anything", config_ids=[]), [])
            ensure.assert_not_called()

    def test_disabled_ai_does_not_produce_fake_embeddings(self):
        with self.assertRaises(ProviderUnavailable):
            AIService.get_embedding("text")
