from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient
from api.testing.factories import setup_case, client_for, candidate_for
from api.models import (
    AuthSession,
    TeamMembership,
    ClientProjectAccess,
    UserProfile,
    OutboxEvent,
)
from api.authentication import rotate_refresh, SessionJWTAuthentication
from api import otp


class AuthenticationAndAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        setup_case(self)

    def test_all_business_endpoints_require_authentication(self):
        anonymous = APIClient()
        for path in (
            "projects",
            "profile",
            "notifications",
            "kpi/summary",
            "candidates",
            "commitments",
            "finance",
            "directory",
            "operations-health",
            "auth/sessions",
        ):
            with self.subTest(path=path):
                self.assertEqual(anonymous.get(f"/api/{path}/").status_code, 401)

    def test_phone_without_invitation_never_gets_account_or_business_access(self):
        anon = APIClient()
        with patch("api.otp.secrets.randbelow", return_value=2345):
            self.assertEqual(
                anon.post("/api/auth/send-code/", {"phone": "77099999999"}).status_code,
                200,
            )
        self.assertFalse(OutboxEvent.objects.filter(event_type="otp").exists())
        self.assertEqual(
            anon.post(
                "/api/auth/verify-code/", {"phone": "77099999999", "code": "3345"}
            ).status_code,
            401,
        )
        self.assertFalse(User.objects.filter(username="77099999999").exists())

    def test_otp_single_use_and_attempt_limit(self):
        with patch("api.otp.secrets.randbelow", return_value=2345):
            delivery = otp.issue(self.manager.username, "127.0.0.1")
        self.assertEqual(otp.delivery_code(delivery), "3345")
        self.assertTrue(otp.verify(self.manager.username, "3345"))
        self.assertFalse(otp.verify(self.manager.username, "3345"))
        cache.clear()
        with patch("api.otp.secrets.randbelow", return_value=2345):
            otp.issue(self.manager.username, "127.0.0.1")
        for _ in range(5):
            self.assertFalse(otp.verify(self.manager.username, "9999"))
        self.assertFalse(otp.verify(self.manager.username, "3345"))

    def test_issuance_quota_persists_after_new_code(self):
        with override_settings(OTP_PHONE_HOUR_LIMIT=2):
            for _ in range(2):
                cache.delete(f"otp:cool:{self.manager.username}")
                self.assertIsNotNone(otp.issue(self.manager.username, "127.0.0.1"))
            cache.delete(f"otp:cool:{self.manager.username}")
            self.assertIsNone(otp.issue(self.manager.username, "127.0.0.1"))

    def test_revocation_blocks_existing_access_immediately(self):
        client = client_for(self.manager)
        self.assertEqual(client.get("/api/profile/").status_code, 200)
        AuthSession.objects.filter(pk=client.test_session.id).update(
            revoked_at=timezone.now()
        )
        self.assertEqual(client.get("/api/profile/").status_code, 401)

    def test_refresh_replay_revokes_family(self):
        client = client_for(self.manager)
        _, _, new_refresh = rotate_refresh(client.test_refresh)
        self.assertNotEqual(new_refresh, client.test_refresh)
        with self.assertRaises(Exception):
            rotate_refresh(client.test_refresh)
        client.test_session.refresh_from_db()
        self.assertIsNotNone(client.test_session.revoked_at)
        self.assertEqual(client.get("/api/profile/").status_code, 401)

    def test_refresh_requires_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        client.cookies["mazory_refresh"] = self.client.test_refresh
        self.assertEqual(client.post("/api/auth/refresh/").status_code, 403)

    def test_manager_scope_and_no_role_escalation(self):
        client = client_for(self.manager)
        results = client.get("/api/projects/").json()["results"]
        self.assertEqual([p["id"] for p in results], [self.project.id])
        self.assertEqual(
            client.get(f"/api/projects/{self.foreign.id}/history/").status_code, 404
        )
        for field in (
            "monthly_target",
            "current_sales",
            "rank_in_team",
            "role",
            "department",
            "phone",
        ):
            self.assertEqual(
                client.put("/api/profile/", {field: "999"}, format="json").status_code,
                400,
            )
        self.assertEqual(client.get("/api/operations-health/").status_code, 403)
        self.assertEqual(
            client.post(
                "/api/whatsapp/send/",
                {
                    "recipient_id": self.other.id,
                    "title": "x",
                    "message": "x",
                    "idempotency_key": "one",
                },
                format="json",
            ).status_code,
            403,
        )

    def test_client_whitelist(self):
        user = User.objects.create_user("77000000005")
        ClientProjectAccess.objects.create(
            user=user, project=self.project, status="active"
        )
        client = client_for(user)
        row = client.get("/api/projects/").json()["results"][0]
        self.assertEqual(set(row), {"id", "name", "status", "version"})
        self.assertEqual(client.get("/api/kpi/summary/").status_code, 403)
        self.assertEqual(
            client.get(f"/api/projects/{self.project.id}/history/").status_code, 404
        )

    def test_membership_revocation_blocks_session(self):
        client = client_for(self.manager)
        TeamMembership.objects.filter(user=self.manager).update(status="revoked")
        self.assertEqual(client.get("/api/projects/").status_code, 401)
