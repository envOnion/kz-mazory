from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from api.models import UserProfile, Project, Company
from api.datamart import DataMartService

User = get_user_model()


class DataMartKpiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testmanager', password='password123')
        self.profile = UserProfile.objects.create(
            user=self.user,
            full_name='Улугбек Тестовый',
            role='Менеджер по продажам',
            bitrix_user_id='34',
            monthly_target=Decimal('60000000.00'),
            current_sales=Decimal('45000000.00')
        )
        self.company = Company.objects.create(name='ТОО ТестСтрой')
        self.project = Project.objects.create(
            name='Строительство БТП ЖК Тест',
            manager=self.profile,
            company=self.company,
            contract_amount=Decimal('45000000.00'),
            cost_amount=Decimal('35000000.00'),
            paid_amount=Decimal('45000000.00'),
            status='completed'
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_margin_overflow_protection(self):
        """Проверка, что экстремальные значения маржи не вызывают numeric field overflow."""
        # 1. Огромная маржа (мизерный контракт, отрицательные затраты или аномалия)
        p1 = Project.objects.create(
            name='Экстремальный проект',
            manager=self.profile,
            contract_amount=Decimal('10.00'),
            cost_amount=Decimal('1000000.00'), # огромный убыток
            paid_amount=Decimal('10.00')
        )
        p1.refresh_from_db()
        # Маржа должна быть ограничена и успешно сохранена
        self.assertTrue(p1.actual_margin_percent <= Decimal('0.00'))
        self.assertTrue(p1.actual_margin_percent >= Decimal('-99999999.99'))

    def test_sales_chart_dataset_real_calculation(self):
        """Проверка, что get_sales_chart_dataset возвращает реальные рассчитанные суммы."""
        chart_data = DataMartService.get_sales_chart_dataset()
        self.assertEqual(chart_data['chart_type'], 'bar')
        self.assertIn('labels', chart_data)
        self.assertIn('Улугбек Тестовый', chart_data['labels'])
        self.assertGreaterEqual(len(chart_data['datasets']), 2)

        # Факт сбора оплат должен соответствовать 45.0 млн ₸
        fact_dataset = next(d for d in chart_data['datasets'] if 'Сбор оплат' in d['label'])
        idx = chart_data['labels'].index('Улугбек Тестовый')
        self.assertEqual(fact_dataset['data'][idx], 45.0)

    def test_sales_kpi_mart_dual_contract_keys(self):
        """Проверка, что get_sales_kpi_mart возвращает и camelCase, и snake_case ключи для фронтенда."""
        kpi_mart = DataMartService.get_sales_kpi_mart()

        # Верхние метрики
        self.assertIn('summary_metrics', kpi_mart)
        self.assertIn('summaryMetrics', kpi_mart)
        self.assertGreater(len(kpi_mart['summaryMetrics']), 0)
        first_metric = kpi_mart['summaryMetrics'][0]
        self.assertIn('value', first_metric)
        self.assertIn('trendPositive', first_metric)

        # Менеджеры
        self.assertIn('managers', kpi_mart)
        self.assertGreater(len(kpi_mart['managers']), 0)
        mgr = kpi_mart['managers'][0]
        # Проверяем наличие обоих стилей именования
        self.assertIn('kpiPercent', mgr)
        self.assertIn('kpi_percent', mgr)
        self.assertIn('salesAmount', mgr)
        self.assertIn('sales_amount', mgr)
        self.assertIn('dealsCount', mgr)
        self.assertIn('deals_count', mgr)
        self.assertIn('statusColor', mgr)
        self.assertIn('kpiBarColor', mgr)
        self.assertIn('trend', mgr)
        self.assertIn('trendPositive', mgr)

        self.assertIn('₸', mgr['salesAmount'])
        self.assertGreater(mgr['kpiPercent'], 0)

    def test_kpi_summary_view_api(self):
        """Проверка API эндпоинта /api/kpi/summary/ с включением графика и метрик."""
        response = self.client.get('/api/kpi/summary/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertIn('chart_data', data)
        self.assertIn('chartData', data)
        self.assertIn('summaryMetrics', data)
        self.assertIn('summary_metrics', data)
        self.assertIn('managers', data)
        self.assertGreater(len(data['managers']), 0)
        self.assertIn('kpiPercent', data['managers'][0])
