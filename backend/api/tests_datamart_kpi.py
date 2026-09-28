from datetime import datetime, date, timedelta, timezone as tz
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from api.testing.factories import setup_case, client_for
from api.models import (
    FinancialRecord,
    SalesTarget,
    PaymentScheduleItem,
    PaymentAllocation,
)
from api.datamart import datamart, period_bounds


class DataMartKpiTests(TestCase):
    def setUp(self):
        setup_case(self)
        self.team.history_complete_from = date(2020, 1, 1)
        self.team.save()
        for day, amount in (
            (date(2026, 9, 1), "100.10"),
            (date(2026, 9, 15), "200.20"),
            (date(2026, 8, 5), "50.05"),
            (date(2026, 7, 1), "-10.00"),
            (date(2026, 1, 1), "25.00"),
        ):
            FinancialRecord.objects.create(
                project=self.project,
                credited_profile=self.manager.profile,
                amount=Decimal(amount),
                payment_date=day,
                is_verified=True,
            )

    @patch(
        "django.utils.timezone.now",
        return_value=datetime(2026, 9, 28, 12, tzinfo=tz.utc),
    )
    def test_exact_periods_and_missing_targets(self, _):
        for period, expected in [
            ("this_month", "300.30"),
            ("last_month", "50.05"),
            ("quarter", "340.35"),
            ("year", "365.35"),
        ]:
            mart = datamart.get_sales_kpi_mart(self.manager, period)
            self.assertEqual(mart["fact"], expected)
            self.assertIsNone(mart["target"])
            self.assertEqual(
                sum(row["amount"] for row in mart["source_rows"]), Decimal(expected)
            )
        SalesTarget.objects.create(
            team=self.team,
            profile=self.manager.profile,
            month=date(2026, 9, 1),
            amount=600,
            approved_by=self.lead,
        )
        mart = datamart.get_sales_kpi_mart(self.manager)
        self.assertEqual(mart["target"], "600.00")
        self.assertEqual(mart["managers"][0]["kpiPercent"], 50.05)

    @patch(
        "django.utils.timezone.now",
        return_value=datetime(2026, 9, 28, 12, tzinfo=tz.utc),
    )
    def test_zero_and_partial_history_are_distinct(self, _):
        FinancialRecord.objects.all().delete()
        mart = datamart.get_sales_kpi_mart(self.manager)
        self.assertEqual(mart["fact"], "0.00")
        self.assertEqual(mart["coverage"]["status"], "complete")
        self.team.history_complete_from = None
        self.team.save()
        mart = datamart.get_sales_kpi_mart(self.manager)
        self.assertEqual(mart["coverage"]["status"], "partial")
        self.assertIsNone(mart["comparison"]["percent"])

    def test_receivable_requires_due_schedule_and_uses_allocations(self):
        today = date(2026, 9, 28)
        overdue = PaymentScheduleItem.objects.create(
            project=self.project,
            amount=500,
            due_date=today - timedelta(days=31),
            is_verified=True,
        )
        PaymentScheduleItem.objects.create(
            project=self.project,
            amount=200,
            due_date=today + timedelta(days=1),
            is_verified=True,
        )
        payment = FinancialRecord.objects.filter(amount__gt=0).first()
        PaymentAllocation.objects.create(
            financial_record=payment, schedule_item=overdue, amount=100
        )
        data = datamart.receivables(self.manager, today=today)
        self.assertEqual(data["overdue"], "400.00")
        self.assertEqual(data["buckets"]["31_60"], "400.00")
        self.assertEqual(data["buckets"]["not_due"], "200.00")

    def test_unknown_cost_is_not_zero_cost(self):
        self.project.cost_confirmed = False
        self.project.save()
        data = datamart.get_pipeline_mart(self.manager)
        self.assertIsNone(data["projects"][0]["margin_percent"])
        self.assertIsNone(data["weighted_margin"])

    def test_other_currency_and_unverified_records_are_excluded(self):
        FinancialRecord.objects.create(
            project=self.project,
            amount=999,
            payment_date=date(2026, 9, 2),
            currency="USD",
            is_verified=True,
        )
        FinancialRecord.objects.create(
            project=self.project,
            amount=999,
            payment_date=date(2026, 9, 2),
            is_verified=False,
        )
        with patch(
            "django.utils.timezone.now",
            return_value=datetime(2026, 9, 28, 12, tzinfo=tz.utc),
        ):
            self.assertEqual(
                datamart.get_sales_kpi_mart(self.manager)["fact"], "300.30"
            )
