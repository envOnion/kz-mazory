from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Team, TeamMembership, UserProfile, OutboxEvent
from .user_admin import ProfileIdentityForm


@override_settings(ALLOWED_HOSTS=["testserver"], CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class UserProvisioningTests(TestCase):
    def setUp(self):
        cache.clear()
        self.root = User.objects.create_superuser("admin", password="test-password")
        self.team = Team.objects.create(name="Test")
        self.client.force_login(self.root)
        self.data = {"username": "8 (999) 123-45-67", "full_name": "Тест", "team": self.team.pk, "role": "manager", "_save": "1"}

    def test_add_links_and_forms(self):
        for url in ("/admin/auth/user/", "/admin/api/userprofile/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, url + "add/")
        self.assertContains(self.client.get("/admin/auth/user/add/"), 'name="team"')

    def test_admin_provisions_complete_identity_without_privileges(self):
        response = self.client.post("/admin/auth/user/add/", self.data)
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="79991234567")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.profile.phone, user.username)
        self.assertEqual(user.profile.full_name, "Тест")
        self.assertTrue(TeamMembership.objects.filter(user=user, team=self.team, role="manager", status="active").exists())
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password("test-password"))
        duplicate = self.client.post("/admin/auth/user/add/", {**self.data, "username": "+7 999 1234567"})
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "уже существует")
        self.assertEqual(User.objects.filter(username="79991234567").count(), 1)

    def test_provisioning_rolls_back_when_membership_fails(self):
        with patch.object(TeamMembership.objects, "create", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                self.client.post("/admin/auth/user/add/", self.data)
        self.assertFalse(User.objects.filter(username="79991234567").exists())
        self.assertFalse(UserProfile.objects.filter(phone="79991234567").exists())

    def test_ordinary_staff_cannot_manage_users_or_profiles(self):
        staff = User.objects.create_user("staff", is_staff=True)
        self.client.force_login(staff)
        for url in ("/admin/auth/user/add/", "/admin/api/userprofile/add/"):
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_profile_creation_and_phone_consistency(self):
        user = User.objects.create_user("79991234567")
        response = self.client.post("/admin/api/userprofile/add/", {
            "user": user.pk, "full_name": "Тест", "phone": "+7 999 1234567",
            "email": "", "department": "", "_save": "1",
        })
        self.assertEqual(response.status_code, 302, response.context["adminform"].form.errors if response.status_code == 200 else "")
        form = ProfileIdentityForm(instance=user.profile, data={"user": user.pk, "full_name": "Тест", "phone": "79997654321", "email": "", "department": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)

    def test_missing_user_returns_error_and_keeps_cooldown(self):
        api = APIClient()
        response = api.post("/api/auth/send-code/", {"phone": "79991234567"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "Пользователя нет в системе"})
        self.assertEqual(api.post("/api/auth/send-code/", {"phone": "79991234567"}).status_code, 429)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_registered_users_access_and_activity(self):
        api = APIClient()
        for index, state, expected in ((1, "active", 200), (2, "no_access", 403), (3, "disabled", 403)):
            phone = f"7999123456{index}"
            user = User.objects.create_user(phone, is_active=state != "disabled")
            if state == "active":
                TeamMembership.objects.create(user=user, team=self.team, role="manager", status="active")
            response = api.post("/api/auth/send-code/", {"phone": phone})
            self.assertEqual(response.status_code, expected)
            self.assertEqual(OutboxEvent.objects.filter(payload__user_id=user.pk).exists(), state == "active")


class AdminSidebarNavigationTests(TestCase):
    def test_personal_settings_is_below_administration(self):
        from django.conf import settings

        navigation = settings.UNFOLD.get("SIDEBAR", {}).get("navigation", [])
        titles = [
            section.get("title")
            for section in navigation
            if isinstance(section, dict) and "title" in section
        ]
        self.assertIn("Администрирование", titles)
        self.assertIn("Личные настройки", titles)
        self.assertGreater(
            titles.index("Личные настройки"),
            titles.index("Администрирование"),
            "Раздел 'Личные настройки' должен находиться ниже раздела 'Администрирование'",
        )

