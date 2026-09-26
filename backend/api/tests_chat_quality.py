from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase
from api.models import Project, BitrixSettings, AISettings, RawMessage
from api.bitrix_service import BitrixService
from api.tasks import process_incoming_message_task


class ChatQualityAndValidationTests(TestCase):
    def setUp(self):
        self.bitrix_cfg = BitrixSettings.objects.create(
            name='Test Bitrix',
            webhook_url='https://aquakip.bitrix24.kz/rest/148/testtoken/',
            is_active=True,
            auto_create_deals=True,
        )
        self.ai_cfg = AISettings.get_active()

    def test_strip_html(self):
        """Проверка утилиты удаления HTML-тегов и сжатия пробелов."""
        self.assertEqual(BitrixService._strip_html(""), "")
        self.assertEqual(BitrixService._strip_html(None), "")
        raw = "Ассалаумагалейкум.<br> Отчёт по <b>ЖК Diamond</b><br />Следующий шаг: КП<p>срочно</p>"
        cleaned = BitrixService._strip_html(raw)
        self.assertNotIn("<br>", cleaned)
        self.assertNotIn("<b>", cleaned)
        self.assertNotIn("<p>", cleaned)
        self.assertEqual(cleaned, "Ассалаумагалейкум. Отчёт по ЖК Diamond Следующий шаг: КП срочно")

    def test_system_prompt_worker_rules(self):
        """Проверка наличия обновленных правил извлечения в промпте воркера."""
        prompt = self.ai_cfg.system_prompt_worker
        self.assertIn("НИКОГДА не копируй текст сообщения целиком ни в одно поле", prompt)
        self.assertIn("120 символов", prompt)
        self.assertIn("contract_amount > 0", prompt)

    @patch('api.tasks.async_task')
    @patch('api.bitrix_service.BitrixService.find_deal_by_name', return_value=None)
    @patch('api.qdrant_service.qdrant_service.upsert_message', return_value='pt-1')
    @patch('api.qdrant_service.qdrant_service.search_similar', return_value=[])
    @patch('api.ai_service.AIService.analyze_message_with_context')
    def test_deal_creation_skipped_when_amount_zero(self, mock_ai, mock_search, mock_upsert, mock_find, mock_async):
        """Сделка не должна создаваться, если contract_amount равен 0 или отсутствует."""
        mock_ai.return_value = {
            "is_deal_fact": True,
            "confidence": 0.95,
            "object_name": "ЖК Diamond",
            "contract_amount": 0,
            "can_create_deal": True,
            "current_action": "Запрос ТЗ",
            "next_action": "Подготовить КП"
        }

        msg_data = {
            "message_id": "test-msg-zero-amt",
            "content": "ЖК Diamond получили ТЗ, готовим КП",
            "sender_name": "Камиль",
            "sender_phone": "77011234567"
        }

        result = process_incoming_message_task(msg_data)
        self.assertEqual(result["status"], "processed")
        self.assertIsNone(result["deal"])
        # Сделка не должна быть создана в БД
        self.assertFalse(Project.objects.filter(name="ЖК Diamond").exists())
        mock_async.assert_not_called()

    @patch('api.tasks.async_task')
    @patch('api.bitrix_service.BitrixService.find_deal_by_name', return_value=None)
    @patch('api.qdrant_service.qdrant_service.upsert_message', return_value='pt-2')
    @patch('api.qdrant_service.qdrant_service.search_similar', return_value=[])
    @patch('api.ai_service.AIService.analyze_message_with_context')
    def test_deal_creation_succeeds_when_amount_positive(self, mock_ai, mock_search, mock_upsert, mock_find, mock_async):
        """Сделка создается успешно при наличии положительного contract_amount."""
        mock_ai.return_value = {
            "is_deal_fact": True,
            "confidence": 0.95,
            "object_name": "ЖК Премиум Тауэр",
            "contract_amount": 42000000.0,
            "can_create_deal": True,
            "current_action": "Согласование КП",
            "next_action": "Подписание договора",
            "stage": "contract_signing"
        }

        msg_data = {
            "message_id": "test-msg-valid-amt",
            "content": "По ЖК Премиум Тауэр утвердили КП на 42 млн ₸, выходим на договор",
            "sender_name": "Жанат",
            "sender_phone": "77019876543"
        }

        result = process_incoming_message_task(msg_data)
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["deal"], "ЖК Премиум Тауэр")

        proj = Project.objects.filter(name="ЖК Премиум Тауэр").first()
        self.assertIsNotNone(proj)
        self.assertEqual(proj.contract_amount, Decimal('42000000.00'))
        mock_async.assert_called_once_with('api.tasks.create_bitrix_deal_task', proj.id)
