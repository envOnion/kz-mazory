"""Read-only provider maintenance probe, executed exclusively by a Q2 task."""

import json
import time
from .ai_service import AIService
from .context_tokens import context_runtime
from .qwen_profile import enabled, parameters, PROFILE
from .providers import ProviderUnavailable


def run():
    cfg = AIService._config()
    if not enabled(cfg):
        raise ProviderUnavailable('provider_profile_mismatch')
    counter, endpoint = context_runtime(cfg)
    payload = parameters({'model': cfg.chat_model_name, 'max_tokens': 1024,
        'messages': [{'role': 'system', 'content': 'Return only a JSON object with one integer field answer.'},
                     {'role': 'user', 'content': 'What is 17 times 23?'}],
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'canary', 'strict': True,
            'schema': {'type': 'object', 'properties': {'answer': {'type': 'integer'}},
                       'required': ['answer'], 'additionalProperties': False}}}})
    base = AIService.effective_chat_provider_url(cfg)
    key = AIService._credential(cfg)
    rendered = AIService._post(base[:-3]+'/apply-template', payload, (5,15), api_key=key, operation='token_count')
    tokens = AIService._post(base[:-3]+'/tokenize', {'content': rendered['prompt'], 'add_special':False}, (5,15), api_key=key, operation='token_count')
    count = counter.count_payload(payload)
    if count != len(tokens['tokens']):
        raise ProviderUnavailable('context_token_count_mismatch')
    tool_payload = parameters({'model': cfg.chat_model_name, 'messages': [
        {'role': 'system', 'content': 'Use tools for Russian business data.'},
        {'role': 'user', 'content': 'Сколько сделок у компании «Альфа»?'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'count-1', 'type': 'function',
            'function': {'name': 'count_deals', 'arguments': '{"company":"Альфа"}'}}]},
        {'role': 'tool', 'tool_call_id': 'count-1', 'content': '{"count":3}'}],
        'tools': [{'type': 'function', 'function': {'name': 'count_deals',
            'description': 'Число сделок компании', 'parameters': {'type': 'object',
                'properties': {'company': {'type': 'string'}}, 'required': ['company']}}}]})
    rendered_tools = AIService._post(base[:-3]+'/apply-template', tool_payload, (5,15), api_key=key, operation='token_count')
    tools_tokens = AIService._post(base[:-3]+'/tokenize', {'content': rendered_tools['prompt'], 'add_special':False}, (5,15), api_key=key, operation='token_count')
    if counter.count_payload(tool_payload) != len(tools_tokens['tokens']):
        raise ProviderUnavailable('context_tool_token_count_mismatch')
    started = time.monotonic()
    response = AIService._post(base+'/chat/completions', payload, (10,120), api_key=key, operation='chat')
    message = response['choices'][0]['message']
    if response['choices'][0]['finish_reason'] != 'stop' or json.loads(message['content']) != {'answer':391} or not message.get('reasoning_content'):
        raise ProviderUnavailable('provider_canary_failed')
    return {'profile': PROFILE, 'model': cfg.chat_model_name, 'context': endpoint['context_length'],
            'input_tokens': count, 'native_count_matches': True, 'native_tool_count_matches': True,
            'reasoning_characters':len(message['reasoning_content']),
            'seconds':round(time.monotonic()-started,3), 'finish_reason':'stop', 'answer':391}
