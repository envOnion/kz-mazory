from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from .admin_forms import AISettingsForm
from .ai_service import AIService
from .models import AISettings
from .providers import ProviderUnavailable, checked_ai_url, checked_base_url, checked_url


@override_settings(PROVIDER_ALLOWED_HOSTS=["crm.example"])
class ProviderURLTests(SimpleTestCase):
    def test_custom_ai_host_is_accepted_in_form_and_runtime(self):
        url = "https://ai.kk-minsk.by/v1"
        for api_format in AISettings.ChatApiFormat.values:
            with self.subTest(api_format=api_format):
                cfg = SimpleNamespace(chat_provider_url=url, chat_api_format=api_format)
                self.assertEqual(AIService.effective_chat_provider_url(cfg), url)
                form = AISettingsForm(instance=AISettings())
                form.cleaned_data = {
                    "chat_api_format": api_format,
                    "chat_provider_url": url + "/",
                    "embedding_provider_url": url,
                }
                form._errors = {}
                form.clean()
                self.assertEqual(form.errors, {})
                self.assertEqual(form.cleaned_data["chat_provider_url"], url)
                self.assertEqual(form.cleaned_data["embedding_provider_url"], url)
        self.assertEqual(checked_ai_url(url + "/chat/completions"), url + "/chat/completions")
        cfg = SimpleNamespace(is_active=True, embedding_provider_url=url)
        with patch.object(AISettings, "get_active", return_value=cfg), patch.object(AIService, "_credential"):
            self.assertIs(AIService._config("embedding"), cfg)

    def test_malformed_ai_base_urls_are_rejected(self):
        for url in (
            "http://ai.kk-minsk.by/v1", "https:///v1", "https://user:pass@example.com/v1",
            "https://example.com:8443/v1", "https://example.com:invalid/v1",
            "https://example.com/v1?", "https://example.com/v1#", "https://example.com/v1;param",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderUnavailable):
                checked_base_url(url)

    def test_crm_host_restriction_is_preserved(self):
        self.assertEqual(checked_url("https://crm.example/rest/1/key/"), "https://crm.example/rest/1/key/")
        with self.assertRaises(ProviderUnavailable):
            checked_url("https://ai.kk-minsk.by/v1")


@override_settings(PROVIDER_ALLOWED_HOSTS=["crm.example"])
class ProviderTransportTests(TestCase):
    def test_custom_host_reaches_transport_for_all_ai_operations(self):
        AISettings.objects.create(name="Custom", is_active=True)
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"usage": {}}
        for api_format, operation, route in (
            ("openai_compatible", "chat", "chat/completions"),
            ("anthropic_messages", "chat", "messages"),
            ("anthropic_messages", "token_count", "messages/count_tokens"),
            ("openai_compatible", "embedding", "embeddings"),
        ):
            with self.subTest(operation=operation, api_format=api_format), patch(
                "api.ai_service.requests.post", return_value=response
            ) as post:
                url = "https://ai.kk-minsk.by/v1/" + route
                AIService._post(url, {}, 5, api_format=api_format, operation=operation, api_key="test-key")
                self.assertEqual(post.call_args.args[0], url)
                self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_metadata_get_uses_authenticated_transport_and_records_usage(self):
        from .models import ProviderUsage

        AISettings.objects.create(name="Metadata", is_active=True)
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"version": "0.34.0"}
        with patch("api.ai_service.requests.get", return_value=response) as get, patch("api.ai_service.requests.post") as post:
            result = AIService._post("https://ollama.example/api/version", {"model": "qwen3.8"}, 5, api_key="test-key", operation="model_metadata", http_method="GET")
        self.assertEqual(result, {"version": "0.34.0"})
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer test-key"})
        self.assertNotIn("json", get.call_args.kwargs)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        post.assert_not_called()
        usage = ProviderUsage.objects.get()
        self.assertTrue(usage.succeeded)
        self.assertEqual(usage.operation, "model_metadata")


@override_settings(AI_DAILY_REQUEST_LIMIT=1, AI_DAILY_BUDGET_USD=1)
class DailyAILimitTests(TestCase):
    def setUp(self):
        from decimal import Decimal
        from .models import ProviderUsage

        self.cfg = AISettings.objects.create(name="Limits", is_active=True)
        from django.contrib.auth.models import User
        User.objects.create(username="limit-admin", is_staff=True)
        ProviderUsage.objects.create(operation="chat", cost_usd=Decimal("10"), succeeded=True, duration_ms=1)
        self.response = Mock(status_code=200, headers={})
        self.response.json.return_value = {"usage": {"cost": "2", "prompt_tokens": 5}}

    def request(self):
        return AIService._post("https://example.com/v1/chat/completions", {}, 5, api_key="test-key")

    def test_zero_limits_ignore_server_defaults_and_preserve_accounting(self):
        from decimal import Decimal
        from .models import ProviderUsage

        with patch("api.ai_service.requests.post", return_value=self.response) as post:
            self.request()
        post.assert_called_once()
        self.assertEqual(ProviderUsage.objects.count(), 2)
        self.assertEqual(ProviderUsage.objects.latest("id").cost_usd, Decimal("2"))

    def test_positive_limits_are_independent_and_exact_threshold_blocks(self):
        from decimal import Decimal

        for count, budget, blocked in [(1, 0, True), (0, 10, True), (2, 0, False), (0, 11, False)]:
            with self.subTest(count=count, budget=budget):
                self.cfg.daily_request_limit = count
                self.cfg.daily_budget_usd = Decimal(budget)
                self.cfg.save()
                with patch("api.ai_service.requests.post", return_value=self.response) as post:
                    if blocked:
                        with self.assertRaisesMessage(ProviderUnavailable, "ai_daily_budget_exhausted"):
                            self.request()
                        post.assert_not_called()
                    else:
                        self.request()
                        post.assert_called_once()
                        from .models import ProviderUsage
                        ProviderUsage.objects.latest("id").delete()

    def alert(self, count=1, cost=10, failed=0):
        from .notifications import plan_risks

        usage = Mock()
        usage.aggregate.return_value = {"total": cost}
        usage.count.return_value = count
        usage.filter.return_value.count.return_value = failed
        with patch("api.models.ProviderUsage.objects.filter", return_value=usage), patch(
            "api.notifications.access.integration_allowed", return_value=True
        ), patch(
            "api.notifications.create_notification"
        ) as notification:
            plan_risks()
        return notification

    def test_zero_limits_do_not_raise_usage_alerts(self):
        self.alert(count=10000, cost=10000).assert_not_called()

    def test_positive_active_limits_raise_alerts_without_server_fallback(self):
        self.cfg.daily_request_limit = 10
        self.cfg.save()
        self.alert(count=7, cost=10000).assert_not_called()
        self.alert(count=8, cost=0).assert_called_once()
        self.cfg.daily_request_limit = 0
        self.cfg.daily_budget_usd = 10
        self.cfg.save()
        self.alert(count=10000, cost=7).assert_not_called()
        self.alert(count=0, cost=8).assert_called_once()

    def test_unlimited_config_still_alerts_on_repeated_failures(self):
        self.alert(failed=10).assert_called_once()


class CRMReadRequestTests(SimpleTestCase):
    def test_more_than_16_read_requests_reach_transport(self):
        from .bitrix_service import BitrixService, CrmReadBudget

        budget = CrmReadBudget(clock=lambda: 0)
        cfg = SimpleNamespace(webhook_base="https://crm.example/rest/1/test/")
        with patch.object(BitrixService, "_request", return_value={"result": {}}) as request:
            for i in range(40):
                BitrixService.read_call("crm.company.get", {"id": str(i + 1)}, config=cfg, budget=budget)
        self.assertEqual(request.call_count, 40)
        self.assertEqual(budget.request_count, 40)

    def test_deadline_still_stops_read_requests(self):
        from .bitrix_service import CrmReadBudget

        clock = Mock(return_value=0)
        budget = CrmReadBudget(clock=clock)
        clock.return_value = 60
        with self.assertRaisesMessage(ProviderUnavailable, "crm_match_deadline"):
            budget.next_timeout()

    def test_write_methods_never_reach_transport(self):
        from .bitrix_service import BitrixService, CrmReadBudget

        with patch.object(BitrixService, "_request") as request:
            with self.assertRaisesMessage(ProviderUnavailable, "crm_read_method_forbidden"):
                BitrixService.read_call("crm.deal.add", {}, config=SimpleNamespace(), budget=CrmReadBudget())
        request.assert_not_called()
