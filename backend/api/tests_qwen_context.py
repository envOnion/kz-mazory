import copy
import json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .ai_service import AIService
from .context_tokens import (
    MANIFEST, QWEN_MANIFEST, AnthropicCounter, NativeCounter, QwenCounter,
    context_runtime, extraction_payload, payload_hash, qwen_counter,
)
from .message_context import build_context
from .models import AISettings, Project, RawMessage, Team
from .providers import ProviderUnavailable


def config(**kwargs):
    values = dict(
        chat_model_name="qwen3.8", chat_provider_url="https://ollama.example/v1",
        tokenizer_id=QWEN_MANIFEST["repo"], tokenizer_revision=QWEN_MANIFEST["revision"],
        context_window_tokens=262144, max_completion_tokens=8192, context_safety_tokens=2048,
    )
    values.update(kwargs)
    return AISettings(**values)


def endpoint():
    return {
        "tag": "ollama", "transport": "ollama", "context_length": 262144,
        "api_format": "openai_compatible", "effective_provider_url": "https://ollama.example/v1",
        "supported_parameters": ["max_tokens", "response_format", "reasoning_effort"],
    }


class QwenCounterTests(SimpleTestCase):
    def test_worker_preflight_rejects_actual_count_mismatch(self):
        from .tasks import verify_ai_context

        cfg = config()
        payload = extraction_payload(cfg, endpoint(), "Тестовое сообщение без фактов.", "Проверка подключения", [], [], None, "UTC+06:00")
        expected = qwen_counter().count_payload(payload)
        for actual in (expected, expected + 1):
            with self.subTest(actual=actual), patch.object(AIService, "_config", return_value=cfg), patch("api.context_tokens.context_runtime", return_value=(qwen_counter(), endpoint())), patch.object(AIService, "analyze_payload", return_value=({"facts": []}, {"prompt_tokens": actual}, {"finish_reason": "stop"})):
                if actual == expected:
                    self.assertEqual(verify_ai_context()["input_tokens_actual"], expected)
                else:
                    with self.assertRaisesMessage(ProviderUnavailable, "context_token_count_invalid"):
                        verify_ai_context()

    def test_counts_match_real_ollama_prompt_eval_counts(self):
        # Captured from Ollama 0.34.0 with the verified GGUF vocabulary and renderer.
        fixtures = (
            ("Тестовое сообщение без фактов.", [], 575),
            ("Оплачено 1000 тенге за материалы. Привет 👋 café 中文", [], 584),
            ('  Пустой тест  ', [{"content": 'Предыдущее сообщение\nсо спецсимволами \\"'}], 589),
        )
        for content, history, expected in fixtures:
            with self.subTest(content=content):
                measured = json.loads((Path(__file__).parent / "test_fixtures/ollama_measured_prompt.json").read_text())
                with patch("api.ai_service.WORKER_PROMPT", measured["system"]):
                    payload = extraction_payload(config(), endpoint(), content, "Тест", history, [], None, "Asia/Almaty")
                self.assertEqual(qwen_counter().count_payload(payload), expected)
                self.assertNotIn("provider", payload)
                self.assertNotIn("plugins", payload)
                self.assertNotIn("reasoning", payload)
                self.assertEqual(payload["reasoning_effort"], "none")
                self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_unsupported_thinking_or_tools_cannot_be_undercounted(self):
        payload = extraction_payload(config(), endpoint(), "Тест", "Тест", [], [], None, "UTC")
        for field, value in (("reasoning_effort", "high"), ("tools", [{"type": "function"}]), ("messages", [{"role": "user", "content": "image"}])):
            with self.subTest(field=field), self.assertRaisesMessage(ProviderUnavailable, "context_token_count_unavailable"):
                qwen_counter().count_payload({**payload, field: value})

    def test_invalid_assets_fail_closed(self):
        with patch.dict(QWEN_MANIFEST["files"], {"tokenizer.json": "invalid-checksum"}):
            with self.assertRaisesMessage(ValueError, "Invalid tokenizer assets"):
                QwenCounter()

    def test_nemotron_still_uses_verified_openrouter_endpoints(self):
        cfg = config(chat_model_name=MANIFEST["models"][0], tokenizer_id=MANIFEST["repo"], tokenizer_revision=MANIFEST["revision"])
        verified = {
            "tag": "verified-router", "context_length": 262144,
            "max_completion_tokens": 8192, "supported_parameters": ["max_tokens", "reasoning"],
        }
        cache.clear()
        with patch("api.context_tokens.requests.get") as get, patch.object(AIService, "_post") as post:
            get.return_value.json.return_value = {"data": {"endpoints": [verified]}}
            counter, ep = context_runtime(cfg)
            self.assertEqual(counter.strategy, "nemotron_local_manifest")
            self.assertEqual(ep["tag"], "verified-router")
            self.assertIn("/endpoints", get.call_args.args[0])
            post.assert_not_called()
        cache.clear()

    def test_legacy_protocol_payloads_are_unchanged(self):
        cfg = config(chat_model_name=MANIFEST["models"][0], tokenizer_id=MANIFEST["repo"], tokenizer_revision=MANIFEST["revision"])
        ep = {**endpoint(), "transport": "openrouter", "tag": "verified-router"}
        payload = extraction_payload(cfg, ep, "Text", "Sender", [], [], None, "UTC")
        self.assertEqual(payload["provider"]["only"], ["verified-router"])
        self.assertFalse(payload["reasoning"]["enabled"])
        self.assertGreater(NativeCounter().count_payload(payload), 0)
        cfg.chat_api_format = "anthropic_messages"
        counter, native_endpoint = context_runtime(cfg)
        self.assertIsInstance(counter, AnthropicCounter)
        self.assertEqual(native_endpoint["api_format"], "anthropic_messages")
        payload = extraction_payload(cfg, native_endpoint, "Text", "Sender", [], [], None, "UTC")
        self.assertIn("system", payload)
        self.assertNotIn("reasoning_effort", payload)


class OllamaRuntimeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.data = {
            "model_info": {field: "verified" for field in QWEN_MANIFEST["ollama"]["identity_fields"]},
            "modelfile": "FROM sha256-model\nRENDERER qwen3.8\nPARSER qwen3.5\n",
            "template": "{{ .Prompt }}", "parameters": "num_ctx 262144\n",
        }
        self.data["model_info"].update({"general.architecture": "qwen35", "qwen35.context_length": 262144})
        fingerprint = payload_hash({field: self.data["model_info"][field] for field in QWEN_MANIFEST["ollama"]["identity_fields"]})
        self.manifest_patch = patch.dict(QWEN_MANIFEST["ollama"], vocabulary_sha256=fingerprint)
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)
        self.addCleanup(cache.clear)

    def run_runtime(self, data=None, cfg=None, version="0.34.0"):
        with patch.object(AIService, "_credential", return_value="test-key"), patch.object(AIService, "_post", side_effect=[{"version": version}, data or self.data]) as post:
            value = context_runtime(cfg or config())
        return value, post

    def test_verified_metadata_is_cached_without_openrouter_discovery(self):
        (counter, ep), post = self.run_runtime()
        self.assertIsInstance(counter, QwenCounter)
        self.assertEqual(ep["context_length"], 262144)
        self.assertEqual([call.args[0] for call in post.call_args_list], ["https://ollama.example/api/version", "https://ollama.example/api/show"])
        self.assertEqual(post.call_args_list[0].kwargs["http_method"], "GET")
        self.assertEqual(post.call_args_list[1].args[1], {"model": "qwen3.8", "verbose": True})
        with patch.object(AIService, "_credential", return_value="test-key"), patch.object(AIService, "_post") as post:
            context_runtime(config())
            post.assert_not_called()

    def test_vocabulary_renderer_and_version_must_match(self):
        for field in ("vocabulary", "renderer", "version"):
            with self.subTest(field=field):
                cache.clear()
                data = copy.deepcopy(self.data)
                version = "0.34.0"
                if field == "vocabulary":
                    data["model_info"]["tokenizer.ggml.pre"] = "different"
                elif field == "renderer":
                    data["modelfile"] = "RENDERER qwen3.5"
                else:
                    version = "0.35.0"
                with self.assertRaisesMessage(ProviderUnavailable, "context_tokenizer_unavailable"):
                    self.run_runtime(data=data, version=version)

    def test_server_window_must_cover_requested_budget_even_after_cache(self):
        self.run_runtime(cfg=config(context_window_tokens=131072))
        with self.assertRaisesMessage(ProviderUnavailable, "context_model_window_unavailable"):
            self.run_runtime(cfg=config(context_window_tokens=300000))
        cache.clear()
        data = copy.deepcopy(self.data)
        data["parameters"] = "num_ctx 8192"
        with self.assertRaisesMessage(ProviderUnavailable, "context_model_window_unavailable"):
            self.run_runtime(data=data)

    def test_wrong_revision_is_rejected_before_network(self):
        with patch.object(AIService, "_post") as post:
            with self.assertRaisesMessage(ProviderUnavailable, "context_tokenizer_unavailable"):
                context_runtime(config(tokenizer_revision="unverified"))
            post.assert_not_called()


class QwenHistoryBudgetTests(TestCase):
    def test_large_history_is_bounded_and_partial_boundary_is_preserved(self):
        team = Team.objects.create(name="Tokenizer test")
        project = Project.objects.create(name="Tokenizer project", team=team)
        now = timezone.now()
        for index in range(3):
            RawMessage.objects.create(team=team, project=project, source="test", message_id=str(index), sender_name="Sender", content="История café 中文 👋 " * 500, timestamp=now - timedelta(minutes=3-index), sent_at_known=True)
        raw = RawMessage.objects.create(team=team, project=project, source="test", message_id="target", sender_name="Sender", content="Тест", timestamp=now, sent_at_known=True)
        cfg = config(context_window_tokens=4096, max_completion_tokens=512, context_safety_tokens=128)
        with patch("api.message_context.context_runtime", return_value=(qwen_counter(), endpoint())):
            history, metadata, payload = build_context(raw, cfg, [], raw.id)
        self.assertLessEqual(metadata["input_tokens_preflight"], cfg.context_window_tokens-512-128)
        self.assertEqual(metadata["input_tokens_preflight"], qwen_counter().count_payload(payload))
        self.assertGreater(metadata["partial_messages_count"], 0)
        self.assertFalse(metadata["history_complete_in_request"])
        self.assertEqual(metadata["token_counter"], "qwen38_ollama_local_manifest")
        self.assertGreater(len(history), 0)
