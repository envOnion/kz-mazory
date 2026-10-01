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
