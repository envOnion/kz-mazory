from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.contrib.admin.sites import AdminSite
from api.models import Project, BitrixSettings, BitrixDealChangeLog
from api.bitrix_service import BitrixService
from api.admin import BitrixDealChangeLogAdmin, ProjectAdmin, BitrixDealChangeLogInLine


class BitrixDealAuditLogTests(TestCase):
    def setUp(self):
        self.bitrix_cfg = BitrixSettings.objects.create(
            name='Test Bitrix',
            webhook_url='https://aquakip.bitrix24.kz/rest/148/testtoken/',
            is_active=True,
            auto_create_deals=True,
        )
        self.project = Project.objects.create(
            name='БМК Алатау 15 МВт',
            contract_number='BMK-2026-01',
            status='qualification',
            contract_amount=Decimal('120000000.00'),
            bitrix_id='778899',
            needs_bitrix_sync=True
        )

    @patch('api.bitrix_service.BitrixService.find_deal_by_name', return_value=None)
    @patch('api.bitrix_service.BitrixService.call')
    def test_create_deal_logs_success(self, mock_call, mock_find):
        """Проверка логирования успешного создания сделки в Bitrix24."""
        mock_call.return_value = {"result": 554433}

        payload = {
            "name": "Новая котельная Семей",
            "contract_amount": 45000000.0,
            "direction": "БМК",
            "status": "lead",
            "current_action": "Запрос ТЗ",
            "next_action": "Расчет стоимости",
        }

        deal_id = BitrixService.create_deal(payload, project=self.project, triggered_by="unit_test")
        self.assertEqual(deal_id, "554433")

        # Проверяем, что запись аудита создалась в БД
        log = BitrixDealChangeLog.objects.filter(bitrix_deal_id="554433").first()
        self.assertIsNotNone(log, "BitrixDealChangeLog должен быть создан в БД")
        self.assertEqual(log.action, "create")
        self.assertEqual(log.status, "success")
        self.assertEqual(log.project, self.project)
        self.assertEqual(log.triggered_by, "unit_test")
        self.assertEqual(log.payload.get("TITLE"), "Новая котельная Семей")
        self.assertEqual(log.response_data.get("result"), 554433)
        self.assertIn("TITLE", log.changed_fields)
        self.assertIn("OPPORTUNITY", log.changed_fields)
        self.assertGreaterEqual(log.duration_ms, 0)
        self.assertEqual(log.error_message, "")

    @patch('api.bitrix_service.BitrixService.find_deal_by_name', return_value=None)
    @patch('api.bitrix_service.BitrixService.call', side_effect=RuntimeError("Bitrix API error: Invalid field"))
    def test_create_deal_logs_error(self, mock_call, mock_find):
        """Проверка логирования сбоя создания сделки в Bitrix24."""
        payload = {
            "name": "Сбойный объект",
            "contract_amount": 1000000.0,
        }

        deal_id = BitrixService.create_deal(payload, project=self.project, triggered_by="error_test")
        self.assertIsNone(deal_id)

        # Проверяем создание лога с ошибкой
        log = BitrixDealChangeLog.objects.filter(action="create", status="error").first()
        self.assertIsNotNone(log)
        self.assertIn("Invalid field", log.error_message)
        self.assertEqual(log.project, self.project)
        self.assertEqual(log.triggered_by, "error_test")

    @patch('api.bitrix_service.BitrixService.call')
    def test_update_deal_logs_success(self, mock_call):
        """Проверка логирования успешного обновления сделки в Bitrix24."""
        mock_call.return_value = {"result": True}

        update_fields = {
            "STAGE_ID": "IN_EXECUTION",
            "OPPORTUNITY": 135000000.0,
            "COMMENTS": "Сделка переведена в производство"
        }

        ok = BitrixService.update_deal("778899", update_fields, project=self.project, triggered_by="manual_sync")
        self.assertTrue(ok)

        log = BitrixDealChangeLog.objects.filter(bitrix_deal_id="778899", action="update").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "success")
        self.assertEqual(log.project, self.project)
        self.assertEqual(log.triggered_by, "manual_sync")
        self.assertEqual(log.payload.get("STAGE_ID"), "IN_EXECUTION")
        self.assertEqual(set(log.changed_fields), {"STAGE_ID", "OPPORTUNITY", "COMMENTS"})
        self.assertGreaterEqual(log.duration_ms, 0)
        self.assertEqual(log.error_message, "")

    @patch('api.bitrix_service.BitrixService.call', side_effect=Exception("Connection reset by peer"))
    def test_update_deal_logs_error(self, mock_call):
        """Проверка логирования ошибки при обновлении сделки."""
        update_fields = {"STAGE_ID": "WON"}

        ok = BitrixService.update_deal("778899", update_fields, project=self.project, triggered_by="qcluster")
        self.assertFalse(ok)

        log = BitrixDealChangeLog.objects.filter(bitrix_deal_id="778899", action="update", status="error").first()
        self.assertIsNotNone(log)
        self.assertIn("Connection reset by peer", log.error_message)
        self.assertEqual(log.project, self.project)

    @patch('api.bitrix_service.BitrixDealChangeLog.objects.create', side_effect=Exception("Database lock"))
    @patch('api.bitrix_service.BitrixService.call', return_value={"result": True})
    def test_logging_failure_isolation(self, mock_call, mock_log_create):
        """Проверка, что сбой записи лога не прерывает саму операцию update_deal."""
        ok = BitrixService.update_deal("778899", {"STAGE_ID": "DESIGN"})
        self.assertTrue(ok, "update_deal должен возвращать True даже если логирование вызвало исключение")

    def test_admin_configuration(self):
        """Проверка конфигурации Django Admin для аудита сделок."""
        site = AdminSite()
        log_admin = BitrixDealChangeLogAdmin(BitrixDealChangeLog, site)
        proj_admin = ProjectAdmin(Project, site)

        # Проверка прав: запрет ручного добавления и редактирования
        self.assertFalse(log_admin.has_add_permission(None))
        self.assertFalse(log_admin.has_change_permission(None))

        # Проверка наличия Inline в ProjectAdmin
        self.assertIn(BitrixDealChangeLogInLine, proj_admin.inlines)

        # Проверка форматирования JSON и ссылок
        log = BitrixDealChangeLog.objects.create(
            project=self.project,
            bitrix_deal_id="778899",
            action="update",
            status="success",
            payload={"TEST_KEY": "TEST_VAL"},
            response_data={"result": True},
            changed_fields=["TEST_KEY"],
            duration_ms=125,
            triggered_by="admin"
        )

        formatted_payload = log_admin.formatted_payload(log)
        self.assertIn("TEST_KEY", formatted_payload)
        self.assertIn("<pre", formatted_payload)

        badge = log_admin.action_badge(log)
        self.assertIn("indigo", badge)

        link = log_admin.bitrix_deal_link(log)
        self.assertIn("778899", link)
