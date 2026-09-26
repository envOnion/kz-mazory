from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch, MagicMock

from api.models import UserProfile, Project, Commitment, FinancialRecord, Company, RawMessage
from api.datamart import DataMartService
from api.tasks import (
    process_incoming_message_task,
    create_bitrix_deal_task,
    sync_single_deal_to_bitrix_task
)
from api.admin import ProjectAdmin, CommitmentAdmin, FinancialRecordAdmin
from django.contrib.admin.sites import AdminSite

User = get_user_model()


class VerificationFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testmanager', password='password123')
        self.profile = UserProfile.objects.create(
            user=self.user,
            full_name='Камиль Тестовый',
            role='Ведущий менеджер',
            bitrix_user_id='101',
            monthly_target=Decimal('50000000.00'),
            current_sales=Decimal('0.00')
        )
        self.company = Company.objects.create(name='ТОО АкваТест')

        # Проверенная сделка
        self.verified_project = Project.objects.create(
            name='Проверенный объект БТП',
            manager=self.profile,
            company=self.company,
            contract_amount=Decimal('40000000.00'),
            cost_amount=Decimal('30000000.00'),
            paid_amount=Decimal('20000000.00'),
            status='in_execution',
            is_verified=True
        )

        # Непроверенная сделка (из чата)
        self.unverified_project = Project.objects.create(
            name='Сырой объект из чата',
            manager=self.profile,
            company=self.company,
            contract_amount=Decimal('80000000.00'),
            cost_amount=Decimal('60000000.00'),
            paid_amount=Decimal('35000000.00'),
            status='proposal_sent',
            is_verified=False
        )

        # Проверенное обязательство
        self.verified_commitment = Commitment.objects.create(
            project=self.verified_project,
            manager=self.profile,
            commitment_text='Согласовать спецификацию',
            deadline=timezone.now().date() - timedelta(days=1), # Просрочено
            status='pending',
            is_verified=True
        )

        # Непроверенное обязательство
        self.unverified_commitment = Commitment.objects.create(
            project=self.unverified_project,
            manager=self.profile,
            commitment_text='Отправить КП на 80 млн',
            deadline=timezone.now().date() - timedelta(days=2), # Просрочено
            status='pending',
            is_verified=False
        )

        # Проверенная оплата
        self.verified_payment = FinancialRecord.objects.create(
            project=self.verified_project,
            amount=Decimal('20000000.00'),
            payment_date=timezone.now().date(),
            payment_type='advance',
            status='received',
            is_verified=True
        )

        # Непроверенная оплата
        self.unverified_payment = FinancialRecord.objects.create(
            project=self.unverified_project,
            amount=Decimal('35000000.00'),
            payment_date=timezone.now().date(),
            payment_type='milestone',
            status='received',
            is_verified=False
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_default_is_verified_is_true(self):
        """Проверка, что стандартное создание сущностей выставляет is_verified=True."""
        p = Project.objects.create(name='Ручной проект')
        c = Commitment.objects.create(commitment_text='Ручная задача')
        f = FinancialRecord.objects.create(project=p, amount=Decimal('1000.00'), payment_date=timezone.now().date())

        self.assertTrue(p.is_verified)
        self.assertTrue(c.is_verified)
        self.assertTrue(f.is_verified)

    def test_kpi_mart_excludes_unverified_data(self):
        """Витрина get_sales_kpi_mart не должна учитывать непроверенные сделки, оплаты и дедлайны."""
        mart = DataMartService.get_sales_kpi_mart(period='this_month')
        mgr_card = next((m for m in mart['managers'] if m['name'] == 'Камиль Тестовый'), None)
        self.assertIsNotNone(mgr_card)

        # Сделок должно быть 1 (только проверенная), а не 2
        self.assertEqual(mgr_card['deals_count'], 1)
        # Сумма сбора должна быть 20 млн ₸ (только проверенная оплата), а не 55 млн ₸
        self.assertEqual(mgr_card['sales_amount'], 20000000.0)
        # Просроченных дедлайнов должно быть 1 (только проверенное), а не 2
        self.assertEqual(mgr_card['overdue_commitments'], 1)
        # В списке проектов менеджера только проверенный проект
        project_ids = [p['id'] for p in mgr_card['projects']]
        self.assertIn(self.verified_project.id, project_ids)
        self.assertNotIn(self.unverified_project.id, project_ids)

    def test_pipeline_mart_excludes_unverified_data(self):
        """Витрина get_pipeline_mart не должна включать непроверенные проекты в воронку."""
        pipeline = DataMartService.get_pipeline_mart()
        
        # Проверяем, что в списке проектов нет непроверенного
        listed_ids = [p['id'] for p in pipeline['projects']]
        self.assertIn(self.verified_project.id, listed_ids)
        self.assertNotIn(self.unverified_project.id, listed_ids)

        # Стадия 'proposal_sent' (где лежит непроверенный проект) должна иметь volume=0
        prop_stage = next((s for s in pipeline['stages'] if s['code'] == 'proposal_sent'), None)
        self.assertIsNotNone(prop_stage)
        self.assertEqual(prop_stage['count'], 0)
        self.assertEqual(prop_stage['volume'], 0.0)

        # Стадия 'in_execution' должна иметь проверенный контракт
        exec_stage = next((s for s in pipeline['stages'] if s['code'] == 'in_execution'), None)
        self.assertIsNotNone(exec_stage)
        self.assertEqual(exec_stage['count'], 1)
        self.assertEqual(exec_stage['volume'], 40000000.0)

    def test_commitments_sla_mart_excludes_unverified_data(self):
        """Витрина get_commitments_sla_mart не должна включать непроверенные обязательства."""
        sla_mart = DataMartService.get_commitments_sla_mart()
        
        # Общее количество должно учитывать только проверенные обязательства
        self.assertEqual(sla_mart['total_count'], 1)
        self.assertEqual(sla_mart['overdue_count'], 1)
        
        item_ids = [c['id'] for c in sla_mart['commitments']]
        self.assertIn(self.verified_commitment.id, item_ids)
        self.assertNotIn(self.unverified_commitment.id, item_ids)

    def test_bitrix_sync_postponed_for_unverified_projects(self):
        """Синхронизация в Bitrix24 должна пропускаться для непроверенных сделок."""
        # Создание сделки
        res_create = create_bitrix_deal_task(self.unverified_project.id)
        self.assertEqual(res_create.get('status'), 'unverified_skipped')
        self.assertIsNone(self.unverified_project.bitrix_id)

        # Обновление сделки
        self.unverified_project.bitrix_id = '9999'
        self.unverified_project.save()
        res_sync = sync_single_deal_to_bitrix_task(self.unverified_project.id)
        self.assertEqual(res_sync.get('status'), 'unverified_skipped')

    @patch('api.views.async_task')
    def test_verify_project_api_endpoint(self, mock_async_task):
        """POST /api/projects/<id>/verify/ должен переводить is_verified в True и триггерить Bitrix24 sync."""
        self.assertFalse(self.unverified_project.is_verified)
        self.unverified_project.bitrix_id = None
        self.unverified_project.save()

        url = f'/api/projects/{self.unverified_project.id}/verify/'
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('status'), 'verified')

        self.unverified_project.refresh_from_db()
        self.assertTrue(self.unverified_project.is_verified)

        # Должна быть вызвана задача создания сделки в Bitrix24
        mock_async_task.assert_called_with('api.tasks.create_bitrix_deal_task', self.unverified_project.id)

        # Теперь после проверки сделка попадает в аналитику
        pipeline = DataMartService.get_pipeline_mart()
        listed_ids = [p['id'] for p in pipeline['projects']]
        self.assertIn(self.unverified_project.id, listed_ids)

    @patch('api.qdrant_service.qdrant_service.search_similar', return_value=[])
    @patch('api.qdrant_service.qdrant_service.upsert_message', return_value='pt-123')
    @patch('api.ai_service.AIService.analyze_message_with_context')
    def test_process_incoming_message_marks_entities_unverified(self, mock_ai, mock_upsert, mock_search):
        """Обработка входящего сообщения WhatsApp AI должна создавать записи с is_verified=False."""
        mock_ai.return_value = {
            "object_name": "Новый Жилой Комплекс Астана",
            "company_name": "BI Group",
            "responsible_name": "Камиль Тестовый",
            "contract_amount": 95000000.0,
            "cost_amount": 75000000.0,
            "paid_amount": 25000000.0,
            "stage": "proposal_sent",
            "next_action": "Согласовать проект до конца недели",
            "can_create_deal": True,
            "confidence": 0.95
        }

        msg_data = {
            "message_id": "test-msg-verification-001",
            "content": "BI Group согласовали сумму 95 млн по ЖК Астана, оплатили 25 млн аванс",
            "sender_name": "Камиль Тестовый",
            "sender_phone": "77011234567"
        }

        result = process_incoming_message_task(msg_data)
        self.assertEqual(result.get('status'), 'processed')

        # Проверяем созданный проект
        created_project = Project.objects.filter(name="Новый Жилой Комплекс Астана").first()
        self.assertIsNotNone(created_project)
        self.assertFalse(created_project.is_verified, "Новая сделка из чата должна быть is_verified=False")
        self.assertTrue(created_project.needs_bitrix_sync)

        # Проверяем созданное обязательство
        created_commitment = Commitment.objects.filter(project=created_project).first()
        self.assertIsNotNone(created_commitment)
        self.assertFalse(created_commitment.is_verified, "Обязательство из чата должно быть is_verified=False")

        # Проверяем созданную оплату
        created_financial = FinancialRecord.objects.filter(project=created_project).first()
        self.assertIsNotNone(created_financial)
        self.assertFalse(created_financial.is_verified, "Оплата из чата должна быть is_verified=False")

    def test_admin_mark_as_verified_and_unverified_actions(self):
        """Проверка работы Admin Actions для верификации и снятия верификации."""
        site = AdminSite()
        project_admin = ProjectAdmin(Project, site)

        # Проверяем mark_as_verified
        mock_request = MagicMock()
        queryset = Project.objects.filter(id=self.unverified_project.id)
        project_admin.mark_as_verified(mock_request, queryset)

        self.unverified_project.refresh_from_db()
        self.assertTrue(self.unverified_project.is_verified)

        # Проверяем mark_as_unverified
        project_admin.mark_as_unverified(mock_request, queryset)
        self.unverified_project.refresh_from_db()
        self.assertFalse(self.unverified_project.is_verified)
