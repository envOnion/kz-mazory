import copy
import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .ai_service import AIService
from .context_tokens import GEMMA_MANIFEST, GemmaCounter, context_runtime, extraction_payload, gemma_counter, payload_hash
from .models import AISettings, ProviderUsage, RawMessage, Team, Project
from .ollama_chat import analytics_request, native_request, normalize_response
from .providers import ProviderUnavailable


def config(**changes):
    values = dict(chat_model_name='gemma4:e4b', chat_provider_url='https://ollama.example/v1', tokenizer_id=GEMMA_MANIFEST['repo'], tokenizer_revision=GEMMA_MANIFEST['revision'], context_window_tokens=131072, max_completion_tokens=8192, context_safety_tokens=2048)
    return AISettings(**{**values, **changes})


def endpoint():
    return {'tag': 'ollama', 'transport': 'ollama_native', 'context_length': 131072, 'supported_parameters': ['max_tokens', 'response_format', 'reasoning_effort'], 'api_format': 'openai_compatible', 'effective_provider_url': 'https://ollama.example/v1'}


def payload(cfg=None):
    return extraction_payload(cfg or config(), endpoint(), 'Тест café 中文 👋', 'Тест', [], [], None, 'UTC')


def response(**changes):
    return {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"facts":[]}'}, 'prompt_eval_count': 900, 'eval_count': 5, **changes}


class GemmaRuntimeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.data = {'model_info': {field: 'verified' for field in GEMMA_MANIFEST['ollama']['identity_fields']}, 'template': '{{ .Prompt }}', 'parameters': 'temperature 1', 'modelfile': 'RENDERER gemma4\n'}
        self.data['model_info'].update({'general.architecture': 'gemma4', 'gemma4.context_length': 131072})
        fingerprint = payload_hash({field: self.data['model_info'][field] for field in GEMMA_MANIFEST['ollama']['identity_fields']})
        self.profile = patch.dict(GEMMA_MANIFEST['ollama'], vocabulary_sha256=fingerprint)
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def runtime(self, cfg=None, data=None, version='0.34.0'):
        with patch.object(AIService, '_credential', return_value='fixture-key'), patch.object(AIService, '_post', side_effect=[{'version': version}, data or self.data]):
            return context_runtime(cfg or config())

    def test_native_window_is_explicit_without_modelfile_num_ctx(self):
        counter, ep = self.runtime()
        self.assertIsInstance(counter, GemmaCounter)
        self.assertEqual(ep['context_length'], 131072)
        request = native_request(payload(), config())
        self.assertEqual(request['options']['num_ctx'], 131072)
        self.assertEqual(request['options']['num_predict'], 8192)
        self.assertFalse(request['think'])
        self.assertFalse(request['stream'])
        self.assertEqual(request['format'], 'json')

    def test_cached_model_window_cannot_be_exceeded(self):
        self.runtime()
        with self.assertRaisesMessage(ProviderUnavailable, 'context_model_window_unavailable'):
            self.runtime(config(context_window_tokens=262144))

    def test_vocab_renderer_version_and_small_window_are_rejected(self):
        for field in ('vocabulary', 'renderer', 'version', 'window'):
            with self.subTest(field=field):
                cache.clear()
                data = copy.deepcopy(self.data)
                version = '0.34.0'
                if field == 'vocabulary': data['model_info']['tokenizer.ggml.model'] = 'different'
                if field == 'renderer': data['modelfile'] = 'RENDERER gemma4-large'
                if field == 'version': version = 'unverified'
                if field == 'window': data['model_info']['gemma4.context_length'] = 4096
                with self.assertRaises(ProviderUnavailable): self.runtime(data=data, version=version)

    def test_qwen_or_wrong_gemma_revision_cannot_be_used(self):
        for changes in ({'tokenizer_id': 'Qwen/Qwen3.8-27B'}, {'tokenizer_revision': 'unknown'}):
            with self.subTest(changes=changes), patch.object(AIService, '_post') as post:
                with self.assertRaisesMessage(ProviderUnavailable, 'context_tokenizer_unavailable'):
                    context_runtime(config(**changes))
                post.assert_not_called()

    def test_assets_must_match_checksums(self):
        with patch.dict(GEMMA_MANIFEST['files'], {'tokenizer.json': 'bad'}):
            with self.assertRaisesMessage(ValueError, 'Invalid tokenizer assets'): GemmaCounter()


class GemmaTransportTests(SimpleTestCase):
    def test_counts_match_native_ollama_measurements(self):
        fixture = json.loads((Path(__file__).parent / 'test_fixtures/gemma4_measured_prompts.json').read_text())
        for case in fixture['cases']:
            with self.subTest(count=case['prompt_eval_count']):
                self.assertEqual(gemma_counter().count_payload(case['payload']), case['prompt_eval_count'])

    def test_tools_images_thinking_and_extra_roles_are_not_undercounted(self):
        for changes in ({'tools': [{'type': 'function'}]}, {'reasoning_effort': 'high'}, {'messages': [{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'x', 'images': ['x']}]}, {'messages': [{'role': 'user', 'content': 'x'}]}):
            with self.subTest(changes=changes), self.assertRaisesMessage(ProviderUnavailable, 'context_token_count_unavailable'):
                gemma_counter().count_payload({**payload(), **changes})

    def test_changed_window_or_model_is_rejected_before_generation(self):
        for changes in ({'_ollama_num_ctx': 262144}, {'model': 'qwen3.8'}):
            with self.subTest(changes=changes), self.assertRaisesMessage(ProviderUnavailable, 'context_configuration_changed'):
                native_request({**payload(), **changes}, config())

    def test_input_cannot_consume_answer_reserve(self):
        cfg = config(context_window_tokens=1200, max_completion_tokens=1024, context_safety_tokens=128)
        with self.assertRaisesMessage(ProviderUnavailable, 'context_budget_exceeded'):
            native_request(payload(cfg), cfg)

    def test_native_response_preserves_exact_usage_and_truncation(self):
        self.assertEqual(normalize_response(response())['usage'], {'prompt_tokens': 900, 'completion_tokens': 5})
        self.assertEqual(normalize_response(response(done_reason='length'))['choices'][0]['finish_reason'], 'length')
        for changes in ({'done': False}, {'prompt_eval_count': True}, {'eval_count': -1}, {'message': {'role': 'assistant', 'content': []}}):
            with self.subTest(changes=changes), self.assertRaisesMessage(ProviderUnavailable, 'provider_invalid_response'):
                normalize_response(response(**changes))

    def test_extraction_uses_native_endpoint_with_existing_credential(self):
        with patch.object(AIService, '_config', return_value=config()), patch.object(AIService, '_credential', return_value='fixture-key'), patch.object(AIService, '_post', return_value=response()) as post:
            result, usage, diagnostics = AIService.analyze_payload(payload())
        self.assertEqual(result, {'facts': []})
        self.assertEqual(usage['prompt_tokens'], 900)
        self.assertEqual(post.call_args.args[0], 'https://ollama.example/api/chat')
        self.assertEqual(post.call_args.kwargs['api_key'], 'fixture-key')
        self.assertNotIn('_ollama_num_ctx', post.call_args.args[1])

    def test_analytics_tool_identity_survives_native_round_trip(self):
        raw = response(message={'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'read_projects', 'arguments': {'limit': 3}}}]})
        normalized = normalize_response(raw, allow_tools=True)
        call = normalized['choices'][0]['message']['tool_calls'][0]
        messages = [{'role': 'system', 'content': 'Read facts'}, {'role': 'user', 'content': 'Projects'}, normalized['choices'][0]['message'], {'role': 'tool', 'tool_call_id': call['id'], 'content': '{"projects":[]}'}]
        with patch('api.context_tokens.context_runtime', return_value=(gemma_counter(), endpoint())):
            converted = analytics_request({'model': 'gemma4:e4b', 'messages': messages, 'tools': [], 'max_tokens': 8192}, config())
        self.assertEqual(converted['messages'][-1]['tool_name'], 'read_projects')
        self.assertEqual(converted['messages'][-2]['tool_calls'][0]['function']['arguments'], {'limit': 3})
        self.assertEqual(converted['options']['num_ctx'], 131072)
        with self.assertRaisesMessage(ProviderUnavailable, 'provider_invalid_response'): normalize_response(raw)

    def test_analytics_and_plain_assistant_also_use_bounded_native_context(self):
        with patch.object(AIService, '_config', return_value=config()), patch.object(AIService, '_credential', return_value='fixture-key'), patch('api.context_tokens.context_runtime', return_value=(gemma_counter(), endpoint())), patch.object(AIService, '_post', return_value=response(message={'role': 'assistant', 'content': 'Нет данных.'})) as post:
            self.assertEqual(AIService.chat_assistant('Тест', {}), 'Нет данных.')
            result = AIService.analytics_turn([{'role': 'system', 'content': 'Test'}, {'role': 'user', 'content': 'Test'}], [], time.monotonic()+30)
        self.assertEqual(result['text'], 'Нет данных.')
        for call in post.call_args_list:
            self.assertEqual(call.args[0], 'https://ollama.example/api/chat')
            self.assertEqual(call.args[1]['options']['num_ctx'], 131072)


class GemmaUsageTests(TestCase):
    def test_history_is_cut_at_token_boundaries_without_consuming_reserves(self):
        from .message_context import build_context

        team = Team.objects.create(name='Gemma test')
        project = Project.objects.create(team=team, name='Gemma test')
        now = timezone.now()
        for index in range(3):
            RawMessage.objects.create(team=team, project=project, source='test', message_id=f'gemma-{index}', content='Қазақстан café 中文 👋 ' * 500, timestamp=now-timedelta(minutes=3-index), sent_at_known=True)
        raw = RawMessage.objects.create(team=team, project=project, source='test', message_id='gemma-target', content='Тест', timestamp=now, sent_at_known=True)
        cfg = config(context_window_tokens=4096, max_completion_tokens=512, context_safety_tokens=128)
        with patch('api.message_context.context_runtime', return_value=(gemma_counter(), endpoint())):
            history, metadata, request = build_context(raw, cfg, [], raw.pk)
        self.assertEqual(metadata['input_tokens_preflight'], gemma_counter().count_payload(request))
        self.assertLessEqual(metadata['input_tokens_preflight'], 4096-512-128)
        self.assertGreater(metadata['partial_messages_count'], 0)
        self.assertFalse(metadata['history_complete_in_request'])
        self.assertGreater(len(history), 0)

    def test_native_provider_usage_is_saved_without_losing_counts(self):
        cfg = config()
        cfg.save()
        with patch.object(AIService, '_config', return_value=cfg), patch('api.ai_service.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.headers = {}
            post.return_value.json.return_value = response()
            AIService._post('https://ollama.example/api/chat', {'model': cfg.chat_model_name, 'options': {'num_predict': 8192}}, (5, 10), api_key='fixture-key', response_validator=normalize_response)
        usage = ProviderUsage.objects.get()
        self.assertEqual(usage.input_tokens, 900)
        self.assertEqual(usage.output_tokens, 5)
        self.assertTrue(usage.succeeded)
