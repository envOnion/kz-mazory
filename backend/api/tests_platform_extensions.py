from datetime import date, datetime, timedelta, timezone as tz
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase, TransactionTestCase, override_settings, Client
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from django.utils import timezone
from api.testing.factories import setup_case, client_for, candidate_for
from api.models import (
    SalesTarget,
    FinancialRecord,
    StageTransition,
    ProjectRevision,
    TeamMembership,
    AdminMFA,
    Commitment,
    UserProfile,
    Notification,
    ProviderUsage,
    ClientProjectAccess,
    RawMessage,
)
from api.datamart import datamart
from api.mfa import encryption, totp
from api.notifications import plan_risks


class PlatformExtensionsTests(TestCase):
    def setUp(self):
        setup_case(self)

    def test_disabled_team_invalidates_client_access_in_existing_session(self):
        user = User.objects.create_user("77000000008")
        ClientProjectAccess.objects.create(
            user=user, project=self.project, status="active"
        )
        client = client_for(user)
        self.assertEqual(client.get("/api/projects/").status_code, 200)
        self.team.is_active = False
        self.team.save(update_fields=["is_active"])
        self.assertEqual(client.get("/api/projects/").status_code, 401)

    @patch(
        "django.utils.timezone.now",
        return_value=datetime(2026, 9, 28, 12, tzinfo=tz.utc),
    )
    def test_forecast_uses_three_closed_months_and_never_changes_actual(self, _):
        FinancialRecord.objects.create(
            project=self.project,
            amount=Decimal("920.00"),
            payment_date=date(2026, 6, 3),
            is_verified=True,
        )
        FinancialRecord.objects.create(
            project=self.project,
            amount=Decimal("200.00"),
            payment_date=date(2026, 9, 3),
            is_verified=True,
        )
        data = datamart.get_sales_kpi_mart(self.manager)
        self.assertEqual(data["fact"], "200.00")
        self.assertEqual(data["forecast"]["amount"], "220.00")
        self.team.history_complete_from = date(2026, 7, 1)
        self.team.save()
        self.assertFalse(
            datamart.get_sales_kpi_mart(self.manager)["forecast"]["available"]
        )

    def test_target_currency_and_team_versions_are_independent(self):
        for currency in ("KZT", "USD"):
            response = self.client.post(
                "/api/finance/targets/",
                {
                    "profile_id": self.manager.profile.id,
                    "team_id": self.team.id,
                    "month": "2026-09-01",
                    "amount": "100.00",
                    "currency": currency,
                    "base_version": 0,
                },
                format="json",
            )
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(SalesTarget.objects.filter(is_active=True).count(), 2)
        denied = client_for(self.other).post(
            "/api/finance/targets/",
            {
                "profile_id": self.manager.profile.id,
                "team_id": self.team.id,
                "month": "2026-09-01",
                "amount": "200",
                "currency": "KZT",
                "base_version": 1,
            },
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

    def test_conversion_does_not_invent_legacy_start_and_measures_dwell(self):
        first = self.project.revisions.get(version=1)
        StageTransition.objects.create(
            project=self.project,
            project_revision=first,
            from_stage="",
            to_stage="lead",
            effective_at=timezone.now() - timedelta(days=5),
        )
        second = ProjectRevision.objects.create(
            project=self.project, version=2, snapshot={}
        )
        StageTransition.objects.create(
            project=self.project,
            project_revision=second,
            from_stage="lead",
            to_stage="completed",
            effective_at=timezone.now() - timedelta(days=2),
        )
        result = datamart.get_pipeline_mart(self.manager)
        self.assertEqual(result["conversion"]["value"], 100)
        self.assertEqual(result["stage_duration_days"][0]["mean_days"], 3)
        self.assertIsNone(datamart.get_pipeline_mart(self.other)["conversion"]["value"])

    def test_reassignment_preserves_payment_attribution_and_cancels_stale_task_version(
        self,
    ):
        new = User.objects.create_user("77000000006")
        profile = UserProfile.objects.create(user=new, full_name="New")
        TeamMembership.objects.create(
            user=new, team=self.team, role="manager", status="active"
        )
        payment = FinancialRecord.objects.create(
            project=self.project,
            credited_profile=self.manager.profile,
            amount=100,
            payment_date=date.today(),
            is_verified=True,
        )
        task = Commitment.objects.create(
            project=self.project,
            manager=self.manager.profile,
            commitment_text="Act",
            version=1,
            is_verified=True,
        )
        result = self.client.post(
            f"/api/projects/{self.project.id}/assign/",
            {
                "manager_id": profile.id,
                "base_version": 1,
                "reason": "Передача проекта",
                "transfer_open_commitments": True,
            },
            format="json",
        )
        self.assertEqual(result.status_code, 200, result.data)
        task.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(task.manager, profile)
        self.assertEqual(task.version, 2)
        self.assertEqual(payment.credited_profile, self.manager.profile)
        self.assertEqual(
            client_for(self.manager)
            .get(f"/api/projects/{self.project.id}/history/")
            .status_code,
            404,
        )

    def test_manual_proposal_keeps_confirmed_project_unchanged(self):
        response = client_for(self.manager).post(
            "/api/candidates/manual/",
            {
                "project_id": self.project.id,
                "reason": "Подтверждение из подписанного договора",
                "changes": {"fact_type": "project", "contract_amount": "1500.00"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_amount, Decimal("1000"))
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(
            RawMessage.objects.get(pk=response.data["evidence"][0]["source_id"]).source,
            "manual",
        )

    def test_risk_routing_preferences_and_deduplication(self):
        self.project.status = "proposal_sent"
        self.project.save()
        type(self.project).objects.filter(pk=self.project.id).update(
            updated_at=timezone.now() - timedelta(days=4)
        )
        profile = self.manager.profile
        profile.notification_preferences = {"stalled_deals": True, "whatsapp": False}
        profile.save()
        plan_risks()
        plan_risks()
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.manager, category="stalled_deals"
            ).count(),
            1,
        )
        self.assertFalse(Notification.objects.filter(recipient=self.other).exists())

    @override_settings(ADMIN_MFA_REQUIRED=True)
    @patch("api.mfa.time.time", return_value=1800000000)
    def test_admin_mfa_gate_enrollment_and_counter_replay(self, _):
        cache.clear()
        client = Client()
        client.force_login(self.lead)
        self.assertEqual(client.get("/admin/").url, "/admin/security/mfa/")
        self.assertEqual(client.get("/admin/security/mfa/").status_code, 200)
        entry = AdminMFA.objects.get(user=self.lead)
        secret = encryption().decrypt(entry.encrypted_secret.encode()).decode()
        code = totp(secret, 1800000000 // 30)
        self.assertEqual(
            client.post("/admin/security/mfa/", {"code": code}).status_code, 302
        )
        self.assertEqual(client.get("/admin/").status_code, 200)
        other = Client()
        other.force_login(self.lead)
        replay = other.post("/admin/security/mfa/", {"code": code})
        self.assertEqual(replay.status_code, 200)
        self.assertNotIn("mfa_user_id", other.session)


class RefundAttributionTests(TestCase):
    def setUp(self):
        setup_case(self)

    def test_refund_keeps_original_manager_after_reassignment(self):
        from api.facts import review

        original = FinancialRecord.objects.create(
            project=self.project,
            amount=100,
            payment_date=date.today(),
            credited_profile=self.manager.profile,
            is_verified=True,
        )
        self.project.manager = self.finance.profile
        self.project.save()
        candidate = candidate_for(
            self,
            data={
                "amount": "-10.00",
                "payment_kind": "reversal",
                "reverses_id": original.id,
            },
        )
        review(candidate.id, self.finance, "approve", base_version=1)
        refund = FinancialRecord.objects.get(reverses=original)
        self.assertEqual(refund.credited_profile_id, original.credited_profile_id)
        self.project.refresh_from_db()
        self.assertEqual(self.project.paid_amount, Decimal("90.00"))


class PrivateAttachmentTests(TransactionTestCase):
    def setUp(self):
        setup_case(self)

    def test_private_attachment_requires_publication_and_safe_download(self):
        client_user = User.objects.create_user("77000000007")
        ClientProjectAccess.objects.create(
            user=client_user, project=self.project, status="active"
        )
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            result = self.client.post(
                "/api/attachments/",
                {
                    "project_id": self.project.id,
                    "file": SimpleUploadedFile(
                        "test.pdf",
                        b"%PDF-1.4\nsynthetic",
                        content_type="application/pdf",
                    ),
                },
                format="multipart",
            )
            self.assertEqual(result.status_code, 202, result.data)
            route = f"/api/attachments/{result.data['id']}/"
            client = client_for(client_user)
            self.assertEqual(client.get(route).status_code, 404)
            self.assertEqual(client_for(self.other).get(route).status_code, 404)
            self.assertEqual(
                self.client.post(
                    route, {"published_to_client": True}, format="json"
                ).status_code,
                200,
            )
            download = client.get(route + "?download=1")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download["Content-Type"], "application/octet-stream")
            self.assertIn("attachment", download["Content-Disposition"])
            self.assertEqual(
                b"".join(download.streaming_content), b"%PDF-1.4\nsynthetic"
            )
