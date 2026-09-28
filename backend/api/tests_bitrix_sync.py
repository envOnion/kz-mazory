from unittest.mock import patch
from django.test import TestCase, override_settings
from api.testing.factories import setup_case
from api.models import BitrixSettings, Project, BitrixDealChangeLog, OutboxEvent
from api.bitrix_service import BitrixService
from api.tasks import run_outbox, dispatch_outbox
from api.deduplication import normalize_deal_name


class BitrixSyncTests(TestCase):
    def setUp(self):
        setup_case(self)

    def test_normalization_is_stable(self):
        value = normalize_deal_name("  ЖК   Alpha  ")
        self.assertEqual(value, normalize_deal_name(value))

    def test_disabled_crm_creates_no_external_effect(self):
        from api.providers import ProviderUnavailable

        with patch("requests.post") as post:
            with self.assertRaisesRegex(ProviderUnavailable, "crm_disabled"):
                BitrixService.sync_project(self.project.id, 1)
            post.assert_not_called()

    @patch("api.bitrix_service.BitrixService.call")
    def test_reconciliation_uses_origin_id_not_fuzzy_title(self, call):
        BitrixSettings.objects.create(is_active=True)
        call.side_effect = [{"result": [{"ID": "123"}]}, {"result": True}]
        with override_settings(
            BITRIX_STAGE_MAP={self.project.status: "CRM_EXACT_STAGE"}
        ):
            BitrixService.sync_project(self.project.id, 1)
        self.assertEqual(
            call.call_args_list[1].args[1]["fields"]["STAGE_ID"], "CRM_EXACT_STAGE"
        )
        self.assertEqual(
            call.call_args_list[0].args[1]["filter"]["=ORIGIN_ID"], str(self.project.id)
        )
        self.assertEqual(call.call_args_list[1].args[0], "crm.deal.update")
        self.project.refresh_from_db()
        self.assertEqual(self.project.bitrix_id, "123")
        self.assertEqual(
            BitrixDealChangeLog.objects.get().triggered_by, "approved_revision:1"
        )

    @patch("api.bitrix_service.BitrixService.call")
    def test_stale_project_revision_is_never_published(self, call):
        BitrixSettings.objects.create(is_active=True)
        BitrixService.sync_project(self.project.id, 0)
        call.assert_not_called()

    @patch("api.bitrix_service.BitrixService.call")
    def test_missing_stage_mapping_blocks_external_write(self, call):
        from api.providers import ProviderUnavailable

        BitrixSettings.objects.create(is_active=True)
        with self.assertRaisesRegex(ProviderUnavailable, "crm_stage_mapping_required"):
            BitrixService.sync_project(self.project.id, 1)
        call.assert_not_called()

    def test_bitrix_webhook_fails_closed(self):
        self.assertEqual(
            self.client.post("/api/bitrix/webhook/", {"id": "1"}).status_code, 503
        )
        BitrixSettings.objects.create(is_active=True)
        with override_settings(BITRIX_INBOUND_TOKEN="required-secret"):
            self.assertEqual(
                self.client.post("/api/bitrix/webhook/", {"id": "1"}).status_code, 403
            )

    @patch("api.tasks.async_task", side_effect=RuntimeError("redis down"))
    def test_commit_before_broker_failure_does_not_lose_event(self, _):
        event = OutboxEvent.objects.create(
            event_type="extract_message", deduplication_key="x", payload={"raw_id": 1}
        )
        dispatch_outbox()
        event.refresh_from_db()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.error_code, "broker_unavailable")
