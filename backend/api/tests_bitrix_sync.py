import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from api.models import UserProfile, Company, Project, Commitment, BitrixSettings, BusinessEvent
from api.deduplication import normalize_deal_name
from api.bitrix_service import BitrixService
from api.tasks import (
    process_incoming_message_task,
    sync_single_deal_to_bitrix_task,
    enqueue_hourly_bitrix_sync_task,
    deduplicate_bitrix_deals_task
)

class BitrixDeduplicationAndSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='test_kamil', password='password123')
        self.profile = UserProfile.objects.create(
            user=self.user,
            full_name='Камиль',
            phone='+77011234567',
            bitrix_user_id='148'
        )
        self.company = Company.objects.create(name='ТОО BI Group', bitrix_company_id='10')
        self.bitrix_cfg = BitrixSettings.objects.create(
            name='Test Bitrix',
            webhook_url='https://aquakip.bitrix24.kz/rest/148/testtoken/',
            is_active=True,
            auto_create_deals=True,
            auto_import_deals=True,
            hourly_sync_enabled=True,
            auto_create_tasks=True,
            sync_timeline_comments=True
        )

    def test_normalize_deal_name(self):
        """Проверка очистки названий сделок для дедупликации."""
        self.assertEqual(normalize_deal_name('ЖК «Медео»'), 'медео')
        self.assertEqual(normalize_deal_name('ЖК Медео'), 'медео')
        self.assertEqual(normalize_deal_name('  медео   '), 'медео')
        self.assertEqual(normalize_deal_name('ТОО БЦ "Нурлы Тау"'), 'нурлы тау')
        self.assertEqual(normalize_deal_name('МЖД Алатау-2'), 'алатау-2')

    @patch('api.ai_service.AIService.analyze_message_with_context')
    @patch('api.qdrant_service.qdrant_service.upsert_message', return_value='point-uuid-1')
    @patch('api.qdrant_service.qdrant_service.search_similar', return_value=[])
    @patch('api.bitrix_service.BitrixService.call')
    def test_prevent_duplicate_creation_when_deal_exists_in_crm(self, mock_bx_call, mock_rag, mock_upsert, mock_ai):
        """
        Проверка: если сделка уже есть в Bitrix24, новая сделка НЕ создается,
        а связывается с существующей и обновляется.
        """
        # Имитируем ответ AI с распознаванием ЖК Медео
        mock_ai.return_value = {
            "object_name": "ЖК Медео",
            "company_name": "ТОО BI Group",
            "responsible_name": "Камиль",
            "contract_amount": 85000000.0,
            "stage": "contract_signing",
            "current_action": "Согласовали спецификацию на БТП",
            "next_action": "Подписать договор",
            "can_create_deal": True,
            "confidence": 0.95
        }

        # Имитируем ответ Bitrix24 crm.deal.list: сделка #410 УЖЕ СУЩЕСТВУЕТ
        def mock_call_side_effect(method, params=None):
            if method == "crm.deal.list":
                return {
                    "result": [{
                        "ID": "410",
                        "TITLE": "ЖК Медео",
                        "OPPORTUNITY": "85000000.00",
                        "STAGE_ID": "EXECUTING",
                        "COMPANY_ID": "10",
                        "ASSIGNED_BY_ID": "148"
                    }]
                }
            if method == "crm.deal.update":
                return {"result": True}
            if method == "crm.deal.add":
                raise AssertionError("crm.deal.add should NOT be called when deal exists in Bitrix24!")
            return {"result": True}

        mock_bx_call.side_effect = mock_call_side_effect

        msg_payload = {
            "message_id": "msg-medeo-001",
            "content": "По ЖК Медео согласовали 85 млн, подписать договор",
            "sender_name": "Камиль",
            "sender_phone": "+77011234567",
            "chat_id": "sales-chat"
        }

        res = process_incoming_message_task(msg_payload)
        self.assertEqual(res["status"], "processed")

        # Проверяем, что локальный проект связался с существующим bitrix_id 410
        project = Project.objects.get(normalized_name='медео')
        self.assertEqual(project.bitrix_id, "410")
        self.assertEqual(project.contract_amount, Decimal('85000000.00'))
        self.assertTrue(project.needs_bitrix_sync)

    @patch('api.bitrix_service.BitrixService.call')
    def test_sync_single_deal_to_bitrix_task(self, mock_bx_call):
        """
        Проверка атомарной задачи обновления сделки, публикации таймлайна и создания задач.
        """
        project = Project.objects.create(
            name="ЖК Алтын Сити",
            normalized_name="алтын сити",
            bitrix_id="501",
            manager=self.profile,
            contract_amount=Decimal('120000000.00'),
            paid_amount=Decimal('40000000.00'),
            current_action="Собрали первый модуль БТП",
            next_action="Оформить накладные на отгрузку",
            blocker="Ждем автокран",
            needs_bitrix_sync=True
        )
        comm = Commitment.objects.create(
            project=project,
            manager=self.profile,
            commitment_text="Оформить накладные на отгрузку",
            deadline=timezone.now().date() + timezone.timedelta(days=1),
            status='pending',
            bitrix_task_id=None
        )

        mock_bx_call.return_value = {
            "result": {
                "task": {"id": "9988"}
            }
        }

        res = sync_single_deal_to_bitrix_task(project.id)
        self.assertEqual(res["status"], "synced")
        self.assertEqual(res["created_tasks_count"], 1)

        project.refresh_from_db()
        comm.refresh_from_db()

        self.assertFalse(project.needs_bitrix_sync)
        self.assertIsNotNone(project.last_bitrix_synced_at)
        self.assertEqual(comm.bitrix_task_id, "9988")

    @patch('api.bitrix_service.BitrixService.fetch_all_paged')
    @patch('api.bitrix_service.BitrixService.call')
    def test_deduplicate_deals_in_crm(self, mock_bx_call, mock_fetch_paged):
        """
        Проверка дедупликации 3 сделок ЖК Медео: удаление 2 дублей и сохранение 1 мастер-сделки.
        """
        mock_fetch_paged.return_value = [
            {"ID": "410", "TITLE": "ЖК Медео", "OPPORTUNITY": "85000000", "DATE_CREATE": "2026-09-25T08:49:00"},
            {"ID": "411", "TITLE": "ЖК Медео", "OPPORTUNITY": "85000000", "DATE_CREATE": "2026-09-25T09:33:00"},
            {"ID": "412", "TITLE": "ЖК Медео", "OPPORTUNITY": "85000000", "DATE_CREATE": "2026-09-25T09:33:05"},
        ]
        mock_bx_call.return_value = {"result": True}

        report = deduplicate_bitrix_deals_task(dry_run=False)

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["duplicate_groups_count"], 1)
        self.assertEqual(report["deleted_count"], 2)
        self.assertEqual(report["merged_groups"][0]["master_id"], "410")
        self.assertEqual(report["merged_groups"][0]["duplicates"], ["411", "412"])

    def test_bitrix_webhook_view(self):
        """Проверка входящего вебхука от Bitrix24."""
        client = Client()
        BitrixSettings.objects.filter(is_active=True).update(inbound_token='test-secret-token')
        with patch('api.views.async_task') as mock_async_task:
            mock_async_task.return_value = 'task-uuid-123'
            response = client.post(
                '/api/bitrix/webhook/',
                data={'event': 'ONCRMDEALADD', 'data[FIELDS][ID]': '7788', 'auth[application_token]': 'test-secret-token'}
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "queued")
            self.assertEqual(data["deal_id"], "7788")
            mock_async_task.assert_called_once_with('api.tasks.import_single_deal_from_bitrix_task', '7788')

    def test_bitrix_webhook_forbidden_on_invalid_token(self):
        """Проверка отклонения несанкционированного вебхука Bitrix24."""
        client = Client()
        BitrixSettings.objects.filter(is_active=True).update(inbound_token='test-secret-token')
        response = client.post(
            '/api/bitrix/webhook/',
            data={'event': 'ONCRMDEALADD', 'data[FIELDS][ID]': '7788', 'auth[application_token]': 'wrong-token'}
        )
        self.assertEqual(response.status_code, 403)

    @patch('api.tasks.async_task')
    def test_enqueue_hourly_bitrix_sync_task(self, mock_async_task):
        """Проверка ежечасного диспетчера."""
        p1 = Project.objects.create(name="Объект 1", normalized_name="объект 1", bitrix_id="101", needs_bitrix_sync=True)
        p2 = Project.objects.create(name="Объект 2", normalized_name="объект 2", bitrix_id="102", needs_bitrix_sync=True)
        p3 = Project.objects.create(name="Объект 3", normalized_name="объект 3", bitrix_id="103", needs_bitrix_sync=False)

        with patch('api.bitrix_service.BitrixService.fetch_all_paged', return_value=[]):
            res = enqueue_hourly_bitrix_sync_task()

        self.assertEqual(res["status"], "enqueued")
        self.assertEqual(res["pending_deals_count"], 2)
        self.assertEqual(mock_async_task.call_count, 2)
