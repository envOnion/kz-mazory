import json
import logging
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.admin.sites import AdminSite
from api.admin import RawMessageAdmin
from api.models import (
    UserProfile, Company, Project, RawMessage, Commitment,
    WhatsAppConfig, AISettings, BitrixSettings
)
from api.bitrix_service import BitrixService
from api.tasks import process_incoming_message_task

logger = logging.getLogger(__name__)

class TestApiEndpoints(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='test_kamil', password='password123')
        self.profile = UserProfile.objects.create(
            user=self.user,
            full_name='Камиль',
            monthly_target=Decimal('50000000.00'),
            current_sales=Decimal('31790000.00')
        )
        self.company = Company.objects.create(name='ТОО Тест Строй')
        self.project = Project.objects.create(
            name='ЖК Тестовый Объект',
            company=self.company,
            manager=self.profile,
            contract_amount=Decimal('45000000.00'),
            cost_amount=Decimal('35000000.00'),
            status='qualification'
        )
        self.refresh = RefreshToken.for_user(self.user)
        self.access_token = str(self.refresh.access_token)
        self.auth_headers = {'HTTP_AUTHORIZATION': f'Bearer {self.access_token}'}

    def test_kpi_summary_unauthorized(self):
        response = self.client.get('/api/kpi/summary/')
        self.assertEqual(response.status_code, 401)

    def test_kpi_summary_authorized(self):
        response = self.client.get('/api/kpi/summary/', **self.auth_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('category_badge', data)
        self.assertIn('query_title', data)
        self.assertIn('summary_metrics', data)
        self.assertIn('managers', data)
        self.assertIn('insight', data)

    def test_project_list_unauthorized(self):
        response = self.client.get('/api/projects/')
        self.assertEqual(response.status_code, 401)

    def test_project_list_authorized(self):
        response = self.client.get('/api/projects/', **self.auth_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(isinstance(data, list))
        self.assertGreaterEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'ЖК Тестовый Объект')

    def test_whatsapp_send_unauthorized(self):
        response = self.client.post('/api/whatsapp/send/', data={'phone': '77011234567', 'message': 'Hello'})
        self.assertEqual(response.status_code, 401)

    def test_whatsapp_send_authorized(self):
        response = self.client.post(
            '/api/whatsapp/send/',
            data=json.dumps({'phone': '77011234567', 'message': 'Hello'}),
            content_type='application/json',
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'queued')

    def test_whatsapp_status_unauthorized(self):
        response = self.client.get('/api/whatsapp/status/')
        self.assertEqual(response.status_code, 401)

    def test_chat_query_unauthorized(self):
        response = self.client.post(
            '/api/chat/query/',
            data=json.dumps({'prompt': 'Покажи график продаж'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 401)

    def test_chat_query_chart(self):
        response = self.client.post(
            '/api/chat/query/',
            data=json.dumps({'prompt': 'Покажи график продаж'}),
            content_type='application/json',
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['widget']['type'], 'chart')

    def test_chat_query_commitments(self):
        response = self.client.post(
            '/api/chat/query/',
            data=json.dumps({'prompt': 'Какие обещания и дедлайны горят?'}),
            content_type='application/json',
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['widget']['type'], 'commitments_list')

    def test_chat_query_pipeline(self):
        response = self.client.post(
            '/api/chat/query/',
            data=json.dumps({'prompt': 'Покажи воронку проектов'}),
            content_type='application/json',
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['widget']['type'], 'project_table')

    def test_chat_query_kpi(self):
        response = self.client.post(
            '/api/chat/query/',
            data=json.dumps({'prompt': 'Покажи KPI команды менеджеров'}),
            content_type='application/json',
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['widget']['type'], 'kpi_grid')


class TestWahaWebhookIngestion(TestCase):
    def setUp(self):
        self.client = Client()
        self.waha_cfg = WhatsAppConfig.objects.create(
            name='AquaKip Team',
            group_jid='120363024823904923@g.us',
            is_active=True
        )
        self.waha_headers = {'HTTP_X_API_KEY': 'mazory-waha-key-2026'}

    def test_waha_webhook_unauthorized(self):
        response = self.client.post(
            '/api/whatsapp/webhook/',
            data=json.dumps({"event": "message", "payload": {"body": "test"}}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 401)

    def test_waha_nested_payload_success(self):
        payload = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "waha-test-msg-001",
                "from": "120363024823904923@g.us",
                "fromMe": False,
                "body": "По объекту ЖК Медео согласовали сумму 85 млн ₸, договор на подписи.",
                "participant": "77011234567@c.us",
                "_data": {
                    "notifyName": "Жанат"
                }
            }
        }
        response = self.client.post(
            '/api/whatsapp/webhook/',
            data=json.dumps(payload),
            content_type='application/json',
            **self.waha_headers
        )
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data['status'], 'queued')
        self.assertEqual(data['message_id'], 'waha-test-msg-001')

    def test_waha_from_me_ignored(self):
        payload = {
            "event": "message",
            "payload": {
                "id": "waha-test-from-me",
                "from": "120363024823904923@g.us",
                "fromMe": True,
                "body": "Сообщение от бота"
            }
        }
        response = self.client.post(
            '/api/whatsapp/webhook/',
            data=json.dumps(payload),
            content_type='application/json',
            **self.waha_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ignored')

    def test_waha_unmonitored_group_ignored(self):
        payload = {
            "event": "message",
            "payload": {
                "id": "waha-test-other-group",
                "from": "999999999999999999@g.us",
                "fromMe": False,
                "body": "Сообщение из чужой группы"
            }
        }
        response = self.client.post(
            '/api/whatsapp/webhook/',
            data=json.dumps(payload),
            content_type='application/json',
            **self.waha_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ignored')


class TestPipelineWorker(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='zhanat', password='pass')
        self.profile = UserProfile.objects.create(
            user=self.user,
            full_name='Жанат Бейсбаев',
            phone='77011234567'
        )
        # Отключаем авто-создание сделок в Bitrix в юнит-тесте воркера, чтобы не спамить
        bx_cfg = BitrixSettings.get_active()
        bx_cfg.auto_create_deals = False
        bx_cfg.save()

    def test_process_incoming_message_task(self):
        message_data = {
            "message_id": f"test-worker-{int(timezone.now().timestamp())}",
            "content": "По объекту ЖК Сарыарка утвердили смету на 120 млн тенге, оборудование БТП. До пятницы отправлю финальный договор заказчику.",
            "sender_name": "Жанат Бейсбаев",
            "sender_phone": "77011234567",
            "chat_id": "120363024823904923@g.us"
        }

        result = process_incoming_message_task(message_data)
        self.assertEqual(result["status"], "processed")

        # Проверяем сохранение RawMessage
        raw = RawMessage.objects.filter(message_id=message_data["message_id"]).first()
        self.assertIsNotNone(raw)
        self.assertTrue(raw.processed)

        # Проверяем создание / обновление проекта
        project = Project.objects.filter(name__icontains="Сарыарка").first()
        self.assertIsNotNone(project)
        self.assertGreater(project.contract_amount, 0)

        # Проверяем фиксацию обязательства
        commitment = Commitment.objects.filter(project=project).first()
        self.assertIsNotNone(commitment)
        self.assertEqual(commitment.manager, self.profile)


class TestBitrixIntegrationLiveSafe(TestCase):
    """
    CRITICAL SAFETY TEST:
    Проверяет боевое создание сделки в Bitrix24 через REST API и
    ОБЯЗАТЕЛЬНО немедленно удаляет созданную тестовую сделку.
    """
    def test_create_and_immediately_delete_deal(self):
        test_deal_payload = {
            "name": "[TEST_MAZORY_AUTO_DELETE] E2E Verification Deal",
            "contract_amount": Decimal("100000.00"),
            "direction": "БТП (Автотест)",
            "deal_period": "Сентябрь 2026",
            "current_action": "Автоматическая проверка API Bitrix24",
            "next_action": "Мгновенное удаление сделки после проверки"
        }

        # 1. Создаем тестовую сделку в Bitrix24
        deal_id = BitrixService.create_deal(test_deal_payload)
        self.assertIsNotNone(deal_id, "BitrixService.create_deal должен вернуть ID созданной сделки")
        logger.info("Successfully created test Bitrix deal ID: %s", deal_id)

        try:
            # Проверяем, что ID валиден
            self.assertTrue(str(deal_id).isdigit(), f"ID сделки должен быть числовым: {deal_id}")
        finally:
            # 2. MANDATORY IMMEDIATE DELETION
            delete_ok = BitrixService.delete_deal(deal_id)
            self.assertTrue(delete_ok, f"BitrixService.delete_deal({deal_id}) должен успешно удалить сделку")
            logger.info("Successfully cleaned up test Bitrix deal ID: %s", deal_id)


class TestRawMessageAdmin(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = RawMessageAdmin(RawMessage, self.site)
        self.admin_user = User.objects.create_superuser(
            username='admin_test',
            email='admin@example.com',
            password='admin_password_123'
        )
        self.client = Client()

    def test_processed_badge_rendering(self):
        msg_processed = RawMessage(content="Тестовое сообщение 1", processed=True)
        badge_processed = self.admin.processed_badge(msg_processed)
        self.assertIn("✓ Обработано", badge_processed)
        self.assertIn("bg-emerald-500/10", badge_processed)

        msg_pending = RawMessage(content="Тестовое сообщение 2", processed=False)
        badge_pending = self.admin.processed_badge(msg_pending)
        self.assertIn("Ожидает", badge_pending)
        self.assertIn("bg-amber-500/10", badge_pending)

    def test_rawmessage_changelist_view(self):
        now = timezone.now()
        RawMessage.objects.create(message_id="msg_001", sender_name="Иван", content="Привет", timestamp=now, processed=True)
        RawMessage.objects.create(message_id="msg_002", sender_name="Олег", content="Запрос", timestamp=now, processed=False)

        self.client.force_login(self.admin_user)
        response = self.client.get('/admin/api/rawmessage/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "✓ Обработано")
        self.assertContains(response, "Ожидает")


class TestOtpVerificationCode(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = Client()

    def test_send_verification_code_generates_random_4digit_code(self):
        from django.core.cache import cache
        from unittest.mock import patch

        with patch('api.auth_views.async_task') as mock_async_task:
            response = self.client.post(
                '/api/auth/send-code/',
                data=json.dumps({'phone': '+7 (777) 458-11-11'}),
                content_type='application/json'
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['status'], 'success')
            self.assertEqual(data['phone'], '77774581111')

            # Проверяем, что в Redis сохранен 4-значный числовой код
            cached_code = cache.get('otp:77774581111')
            self.assertIsNotNone(cached_code)
            self.assertEqual(len(cached_code), 4)
            self.assertTrue(cached_code.isdigit())
            code_int = int(cached_code)
            self.assertGreaterEqual(code_int, 1000)
            self.assertLessEqual(code_int, 9999)

            # Проверяем, что задача поставлена в очередь с правильными аргументами
            mock_async_task.assert_called_once_with(
                'api.tasks.send_sms_verification_code_task',
                '77774581111',
                cached_code
            )

    def test_send_sms_verification_code_task_calls_waha(self):
        from unittest.mock import patch
        from api.tasks import send_sms_verification_code_task

        with patch('api.tasks.send_waha_whatsapp_message_task') as mock_waha:
            mock_waha.return_value = {'status': 'delivered'}
            result = send_sms_verification_code_task('+7 (777) 458-11-11', '7492')
            
            mock_waha.assert_called_once_with(
                '77774581111',
                'Ваш код подтверждения для входа в Mazory AI: 7492\nКод действителен 5 минут.'
            )
            self.assertEqual(result, {'status': 'delivered'})


