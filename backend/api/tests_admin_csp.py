from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from api.security import SecurityHeadersMiddleware


class AdminCSPTests(SimpleTestCase):
    def policy(self, path):
        request = RequestFactory().get(path)
        response = SecurityHeadersMiddleware(lambda _: HttpResponse())(request)
        return response.get("Content-Security-Policy", "")

    @override_settings(MAZORY_ENV="production")
    def test_alpine_compatibility_is_limited_to_admin(self):
        self.assertIn("'unsafe-eval'", self.policy("/admin/login/"))
        for path in ("/api/projects/", "/", "/admin-not-a-real-route/"):
            policy = self.policy(path)
            self.assertNotIn("'unsafe-eval'", policy)
            self.assertIn("object-src 'none'", policy)
            self.assertIn("frame-ancestors 'none'", policy)

    def test_test_browser_receives_the_actual_admin_policy(self):
        with override_settings(MAZORY_ENV="production"):
            production = self.policy("/admin/login/")
        with override_settings(MAZORY_ENV="test"):
            self.assertEqual(self.policy("/admin/login/"), production)
