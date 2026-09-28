import hashlib, hmac, json
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.admin.sites import AdminSite
from django.utils import timezone
from api.testing.factories import setup_case
from api.models import (
    RawMessage,
    FactCandidate,
    FinancialRecord,
    OutboxEvent,
    MessageProcessingTrace,
)
from api.pipeline import extract_message
from api.admin import MessageProcessingTraceAdmin, RawMessageAdmin


@override_settings(WAHA_WEBHOOK_SECRET="unit-webhook-secret")
class PipelineTraceTestCase(TestCase):
    def setUp(self):
        setup_case(self)

    def ingest(self, payload):
        body = json.dumps(payload).encode()
        signature = hmac.new(b"unit-webhook-secret", body, hashlib.sha512).hexdigest()
        return self.client.post(
            "/api/messages/ingest/",
            body,
            content_type="application/json",
            HTTP_X_WEBHOOK_HMAC=signature,
        )

    def payload(self, **extra):
        return {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "one",
                "from": "test@g.us",
                "body": "Оплатили 100 KZT Alpha",
                "timestamp": int(timezone.now().timestamp()),
                "participant": self.manager.username + "@c.us",
                **extra,
            },
        }

    def test_inbox_idempotency_and_source_time(self):
        payload = self.payload()
        for _ in range(10):
            self.assertIn(self.ingest(payload).status_code, (200, 202))
        self.assertEqual(RawMessage.objects.count(), 1)
        self.assertEqual(OutboxEvent.objects.count(), 1)
        raw = RawMessage.objects.get()
        self.assertEqual(
            int(raw.timestamp.timestamp()), payload["payload"]["timestamp"]
        )

    def test_webhook_fails_closed_and_validates_source(self):
        with override_settings(WAHA_WEBHOOK_SECRET=""):
            self.assertEqual(self.ingest(self.payload()).status_code, 503)
        body = json.dumps(self.payload()).encode()
        self.assertEqual(
            self.client.post(
                "/api/messages/ingest/", body, content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.ingest(self.payload(**{"from": "foreign@g.us"})).status_code, 403
        )
        self.assertEqual(
            self.ingest({**self.payload(), "event": "unsupported"}).status_code, 400
        )
        self.config.is_active = False
        self.config.save()
        self.assertEqual(self.ingest(self.payload()).status_code, 403)
        self.assertFalse(RawMessage.objects.exists())

    @patch("api.ai_service.AIService.analyze_message_with_context")
    def test_multiple_facts_only_create_proposals_with_evidence(self, ai):
        self.ingest(self.payload())
        raw = RawMessage.objects.get()
        ai.return_value = {
            "facts": [
                {
                    "fact_type": "payment",
                    "object_name": "Alpha",
                    "amount": "100",
                    "payment_date": timezone.localdate().isoformat(),
                    "evidence": raw.content,
                },
                {
                    "fact_type": "commitment",
                    "object_name": "Alpha",
                    "commitment_text": "Уточнить назначение оплаты",
                    "evidence": raw.content,
                },
            ]
        }
        extract_message(raw.id)
        extract_message(raw.id)
        self.assertEqual(FactCandidate.objects.count(), 2)
        self.assertFalse(FinancialRecord.objects.exists())
        self.assertEqual(MessageProcessingTrace.objects.count(), 1)
        self.assertTrue(
            all(
                c.evidence.get().raw_message_id == raw.id
                for c in FactCandidate.objects.all()
            )
        )

    @patch("api.ai_service.AIService.analyze_message_with_context")
    def test_context_excludes_current_future_and_other_chats(self, ai):
        raw = RawMessage.objects.create(
            config=self.config,
            message_id="now",
            content="Now",
            timestamp=timezone.now(),
        )
        from datetime import timedelta

        past = RawMessage.objects.create(
            config=self.config,
            message_id="past",
            content="Past",
            timestamp=raw.timestamp - timedelta(days=1),
        )
        RawMessage.objects.create(
            config=self.config,
            message_id="future",
            content="Future",
            timestamp=raw.timestamp + timedelta(days=1),
        )
        RawMessage.objects.create(
            message_id="other", content="Foreign", timestamp=past.timestamp
        )
        ai.return_value = {"facts": []}
        extract_message(raw.id)
        context = ai.call_args.args[2]
        self.assertEqual([item["id"] for item in context], [past.id])

    def test_admin_escapes_external_html_in_every_trace_card(self):
        content = '<img src=x onerror="window.__xss=1"><script>alert(1)</script>'
        trace = MessageProcessingTrace.objects.create(
            whatsapp_message_id="html",
            whatsapp_content=content,
            whatsapp_sender_name=content,
            earlier_messages_context=[{"content": content, "sender_name": content}],
            earlier_messages_count=1,
            bitrix_deal_title=content,
            bitrix_raw_deal={"TITLE": content},
            ai_extracted_facts={"value": content},
            result_summary=content,
        )
        admin = MessageProcessingTraceAdmin(MessageProcessingTrace, AdminSite())
        for method in (
            "stage_1_whatsapp_card",
            "stage_2_earlier_messages_card",
            "stage_3_bitrix_card",
            "stage_4_final_record_card",
        ):
            html = str(getattr(admin, method)(trace))
            self.assertNotIn("<script>", html)
            self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", str(admin.stage_1_whatsapp_card(trace)))
