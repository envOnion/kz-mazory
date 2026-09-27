from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.utils import timezone
from api.models import (
    Project, RawMessage, Commitment, FinancialRecord,
    UserProfile, Company, MessageProcessingTrace, AISettings, BitrixSettings
)
from api.tasks import process_incoming_message_task
from api.admin import MessageProcessingTraceAdmin, ProjectAdmin, RawMessageAdmin

class MockSuperUser:
    def has_perm(self, perm):
        return True

class PipelineTraceTestCase(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.factory = RequestFactory()
        self.user = User.objects.create_superuser('admin_test', 'admin@example.com', 'password123')

        self.manager = UserProfile.objects.create(
            user=self.user,
            full_name='Самат Ерланулы',
            phone='+77022223344',
            bitrix_user_id='101'
        )
        self.company = Company.objects.create(
            name='ТОО Строитель Групп',
            client_type='private'
        )

        AISettings.objects.get_or_create(name='Test AI')
        BitrixSettings.objects.get_or_create(name='Test Bitrix', is_active=True)

    @patch('api.qdrant_service.qdrant_service.search_similar')
    @patch('api.ai_service.AIService.analyze_message_with_context')
    @patch('api.bitrix_service.BitrixService.find_deal_by_name')
    def test_process_incoming_message_creates_trace(self, mock_bx_find, mock_ai_analyze, mock_qdrant_search):
        # 1. Настройка моков для этапов пайплайна
        # Этап 2: Сообщения ранее (Qdrant)
        mock_qdrant_search.return_value = [
            {
                "message_id": "prev-100",
                "sender_name": "Камиль",
                "sender_phone": "+77019876543",
                "timestamp": "2026-09-27T10:00:00Z",
                "content": "Обсуждаем тепловые пункты для ЖК Шахар Сити",
                "score": 0.8842
            }
        ]

        # Этап 3: Bitrix24 поиск
        mock_bx_find.return_value = {
            "ID": "7788",
            "TITLE": "ЖК Шахар Сити",
            "OPPORTUNITY": 521000000.0,
            "STAGE_ID": "EXECUTING",
            "COMPANY_ID": "42"
        }

        # Этап 4: AI анализ
        mock_ai_analyze.return_value = {
            "is_deal_fact": True,
            "confidence": 0.95,
            "object_name": "ЖК Шахар Сити",
            "company_name": "ТОО Строитель Групп",
            "direction": "БТП",
            "contract_amount": 521000000.0,
            "cost_amount": 420000000.0,
            "paid_amount": 50000000.0,
            "stage": "in_execution",
            "current_action": "Согласован график поставки оборудования",
            "next_action": "Подписать допсоглашение до 30.09",
            "next_action_at": "2026-09-30T18:00:00",
            "can_create_deal": True,
            "priority": "A++"
        }

        # 2. Вызов функции воркера обработки входящего сообщения
        msg_payload = {
            "message_id": "test-waha-trace-001",
            "content": "ЖК Шахар Сити: согласовали график поставки на 521 млн тенге, оплачено 50 млн аванса. Подписать допсоглашение до 30.09",
            "sender_name": "Самат Ерланулы",
            "sender_phone": "+77022223344",
            "chat_id": "aquakip-sales@g.us"
        }

        res = process_incoming_message_task(msg_payload)
        self.assertEqual(res["status"], "processed")

        # 3. Проверка созданной трассировки MessageProcessingTrace
        trace = MessageProcessingTrace.objects.filter(whatsapp_message_id="test-waha-trace-001").first()
        self.assertIsNotNone(trace, "Трассировка пайплайна должна быть создана")

        # Проверка Этапа 1: «Входные данные WhatsApp»
        self.assertEqual(trace.whatsapp_sender_name, "Самат Ерланулы")
        self.assertEqual(trace.whatsapp_sender_phone, "77022223344")
        self.assertIn("Шахар Сити", trace.whatsapp_content)
        self.assertIsNotNone(trace.raw_message)

        # Проверка Этапа 2: «Зависимые данные из сообщений ранее»
        self.assertEqual(trace.earlier_messages_count, 1)
        self.assertEqual(trace.earlier_messages_context[0]["sender_name"], "Камиль")
        self.assertEqual(trace.earlier_messages_context[0]["score"], 0.8842)

        # Проверка Этапа 3: «Зависимые данные из Bitrix24»
        self.assertEqual(trace.bitrix_matched_deal_id, "7788")
        self.assertEqual(trace.bitrix_deal_title, "ЖК Шахар Сити")
        self.assertEqual(trace.bitrix_deal_stage, "EXECUTING")
        self.assertEqual(trace.bitrix_deal_opportunity, Decimal('521000000.00'))

        # Проверка Этапа 4: «Итоговая запись»
        self.assertEqual(trace.pipeline_action, "matched_bitrix_imported")
        self.assertIsNotNone(trace.project)
        self.assertEqual(trace.project.name, "ЖК Шахар Сити")
        self.assertEqual(trace.ai_confidence, 0.95)
        self.assertIsNotNone(trace.commitment)
        self.assertIn("Подписать допсоглашение", trace.commitment.commitment_text)
        self.assertIsNotNone(trace.financial_record)
        self.assertEqual(trace.financial_record.amount, Decimal('50000000.00'))

        # Проверка текстового резюме цепочки
        self.assertIn("1. Входные данные WhatsApp", trace.result_summary)
        self.assertIn("2. Сообщения ранее", trace.result_summary)
        self.assertIn("3. Bitrix24 CRM", trace.result_summary)
        self.assertIn("4. Итоговая запись", trace.result_summary)

    def test_admin_visual_cards_rendering(self):
        # Создаем тестовую трассировку
        proj = Project.objects.create(
            name="ЖК Алтын",
            contract_amount=Decimal('150000000.00'),
            actual_margin_percent=Decimal('18.50'),
            is_verified=True
        )
        comm = Commitment.objects.create(
            project=proj,
            manager=self.manager,
            commitment_text="Предоставить паспорта оборудования",
            deadline=timezone.now().date()
        )
        fin = FinancialRecord.objects.create(
            project=proj,
            amount=Decimal('30000000.00'),
            payment_date=timezone.now().date(),
            status='received'
        )

        trace = MessageProcessingTrace.objects.create(
            whatsapp_message_id="trace-test-card-view",
            whatsapp_chat_id="aquakip-sales@g.us",
            whatsapp_sender_phone="77022223344",
            whatsapp_sender_name="Самат Ерланулы",
            whatsapp_timestamp=timezone.now(),
            whatsapp_content="ЖК Алтын: предоставить паспорта до конца недели, получено 30 млн",
            whatsapp_raw_payload={"test": "payload_val"},
            earlier_messages_context=[
                {"message_id": "hist-1", "sender_name": "Камиль", "timestamp": "2026-09-26", "content": "По ЖК Алтын готовятся спецификации", "score": 0.85}
            ],
            earlier_messages_count=1,
            bitrix_matched_deal_id="9988",
            bitrix_deal_title="ЖК Алтын БТП",
            bitrix_deal_stage="PREPAYMENT_INVOICE",
            bitrix_deal_opportunity=Decimal('150000000.00'),
            bitrix_search_query="ЖК Алтын",
            ai_extracted_facts={
                "object_name": "ЖК Алтын",
                "contract_amount": 150000000.0,
                "direction": "БТП",
                "stage": "proposal_sent"
            },
            ai_confidence=0.92,
            pipeline_action="updated_deal",
            project=proj,
            commitment=comm,
            financial_record=fin,
            status="success",
            result_summary="1. WhatsApp: Самат\n2. Qdrant: 1 сообщение\n3. Bitrix: Сделка #9988\n4. Итог: Обновлена сделка"
        )

        admin = MessageProcessingTraceAdmin(MessageProcessingTrace, self.site)

        # 1. Проверка баннера сквозного процесса
        banner_html = admin.pipeline_overview_banner(trace)
        self.assertIn("WhatsApp Вход", banner_html)
        self.assertIn("Контекст ранее", banner_html)
        self.assertIn("Данные Bitrix24", banner_html)
        self.assertIn("Итоговая запись", banner_html)

        # 2. Проверка карточки Этапа 1
        s1_html = admin.stage_1_whatsapp_card(trace)
        self.assertIn("Самат Ерланулы", s1_html)
        self.assertIn("77022223344", s1_html)
        self.assertIn("ЖК Алтын: предоставить паспорта", s1_html)
        self.assertIn("payload_val", s1_html)

        # 3. Проверка карточки Этапа 2
        s2_html = admin.stage_2_earlier_messages_card(trace)
        self.assertIn("Камиль", s2_html)
        self.assertIn("85.0%", s2_html)
        self.assertIn("По ЖК Алтын готовятся спецификации", s2_html)

        # 4. Проверка карточки Этапа 3
        s3_html = admin.stage_3_bitrix_card(trace)
        self.assertIn("#9988", s3_html)
        self.assertIn("ЖК Алтын БТП", s3_html)
        self.assertIn("150,000,000.00 ₸", s3_html)

        # 5. Проверка карточки Этапа 4
        s4_html = admin.stage_4_final_record_card(trace)
        self.assertIn("ЖК Алтын", s4_html)
        self.assertIn("Предоставить паспорта оборудования", s4_html)
        self.assertIn("30,000,000.00 ₸", s4_html)
        self.assertIn("92%", s4_html)
