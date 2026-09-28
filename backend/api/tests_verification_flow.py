from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from api.testing.factories import setup_case, candidate_for, client_for
from api.models import (
    FinancialRecord,
    ProjectRevision,
    PaymentScheduleItem,
    PaymentAllocation,
    OutboxEvent,
    FactCandidate,
)
from api.facts import review, allocate_payment
from api.security import Conflict


class VerificationFlowTests(TestCase):
    def setUp(self):
        setup_case(self)

    def test_proposal_has_no_effect_and_approval_is_idempotent(self):
        c = candidate_for(self)
        self.assertFalse(FinancialRecord.objects.exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.version, 1)
        review(c.id, self.lead, "approve", base_version=1)
        review(c.id, self.lead, "approve", base_version=1)
        self.assertEqual(FinancialRecord.objects.count(), 1)
        self.project.refresh_from_db()
        self.assertEqual(self.project.paid_amount, Decimal("100"))
        self.assertEqual(self.project.version, 2)
        self.assertEqual(
            ProjectRevision.objects.filter(project=self.project).count(), 2
        )
        self.assertEqual(OutboxEvent.objects.filter(event_type="crm_sync").count(), 1)

    def test_reject_does_not_mutate_project_and_requires_reason(self):
        c = candidate_for(self)
        with self.assertRaises(ValidationError):
            review(c.id, self.lead, "reject")
        review(c.id, self.lead, "reject", reason="Не подтверждено источником")
        self.assertFalse(FinancialRecord.objects.exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.version, 1)

    def test_conflicting_revision_returns_409(self):
        c = candidate_for(self)
        other = candidate_for(self, key="two")
        review(c.id, self.lead, "approve", base_version=1)
        client = client_for(self.finance)
        response = client.post(
            f"/api/candidates/{other.id}/review/",
            {"action": "approve", "base_version": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FinancialRecord.objects.count(), 1)

    def test_manager_cannot_approve_payment(self):
        c = candidate_for(self)
        with self.assertRaises(PermissionDenied):
            review(c.id, self.manager, "approve", base_version=1)

    def test_cumulative_promise_and_unknown_date_are_not_payments(self):
        for key, data in [
            ("total", {"payment_kind": "cumulative"}),
            ("promise", {"payment_kind": "promise"}),
            ("no_date", {"payment_date": None}),
        ]:
            c = candidate_for(self, data=data, key=key)
            with self.assertRaises(ValidationError):
                review(c.id, self.lead, "approve", base_version=1)
        self.assertFalse(FinancialRecord.objects.exists())

    def test_overpayment_and_reversal_preserve_original_and_allocations(self):
        self.project.contract_amount = 50
        self.project.save()
        c = candidate_for(self)
        review(c.id, self.lead, "approve", base_version=1)
        payment = FinancialRecord.objects.get(candidate=c)
        schedule = PaymentScheduleItem.objects.create(
            project=self.project,
            amount=100,
            due_date=timezone.localdate() - timedelta(days=2),
            is_verified=True,
        )
        allocate_payment(self.lead, payment.id, schedule.id, "100")
        self.project.refresh_from_db()
        self.assertEqual(self.project.due_amount, Decimal("-50"))
        reverse = candidate_for(
            self,
            data={
                "payment_kind": "reversal",
                "amount": "-60",
                "reverses_id": payment.id,
            },
            key="reverse",
        )
        review(reverse.id, self.lead, "approve", base_version=2)
        self.assertEqual(
            FinancialRecord.objects.get(pk=payment.id).amount, Decimal("100")
        )
        self.assertEqual(
            sum(PaymentAllocation.objects.values_list("amount", flat=True)),
            Decimal("40"),
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.paid_amount, Decimal("40"))

    def test_no_direct_verify_bypass(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/verify/", {}, format="json"
        )
        self.assertEqual(response.status_code, 409)
