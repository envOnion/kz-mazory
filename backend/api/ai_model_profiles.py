"""Validated, inert model profiles; uploaded tokenizers never execute remote code."""
import json
from functools import lru_cache

from django.core.exceptions import ValidationError
from jinja2 import StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

MAX_PROFILE_BYTES = 16 * 1024 * 1024
PROFILE_FIELDS = {'version', 'quantization', 'tokenizer', 'chat_template', 'context_window_tokens', 'tokenizer_id', 'tokenizer_revision'}


def validate_profile(profile):
    def invalid(message):
        raise ValidationError({'chat_model_profile': message})
    if profile == {}:
        return
    if not isinstance(profile, dict) or set(profile) - PROFILE_FIELDS or type(profile.get('version')) is not int or profile['version'] != 1:
        invalid('Нужен профиль JSON версии 1 с поддерживаемыми полями.')
    try:
        encoded = json.dumps(profile, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        invalid('Профиль должен содержать корректный JSON без NaN/Infinity.')
    if len(encoded.encode()) > MAX_PROFILE_BYTES:
        invalid('Профиль не должен превышать 16 МБ.')
    if 'context_window_tokens' in profile and (type(profile['context_window_tokens']) is not int or not 1 <= profile['context_window_tokens'] <= 10000000):
        invalid('Окно контекста должно быть целым числом от 1 до 10 000 000.')
    for name in ('tokenizer_id', 'tokenizer_revision'):
        if name in profile and (not isinstance(profile[name], str) or len(profile[name]) > (255 if name == 'tokenizer_id' else 64)):
            invalid('Некорректный идентификатор/версия токенизатора.')
    if 'quantization' in profile:
        q = profile['quantization']
        allowed = {'method', 'bits', 'group_size', 'format', 'description'}
        if not isinstance(q, dict) or set(q) - allowed or any(type(v) not in (str, int, float, bool) for v in q.values()):
            invalid('quantization: допустимы method, bits, group_size, format, description со скалярными значениями.')
    if ('tokenizer' in profile) != ('chat_template' in profile):
        invalid('Загрузите tokenizer и chat_template вместе для точного подсчёта токенов.')
    if 'tokenizer' in profile:
        if not isinstance(profile['tokenizer'], dict) or not isinstance(profile['chat_template'], str) or not 1 <= len(profile['chat_template']) <= 131072:
            invalid('tokenizer должен содержать tokenizer.json, chat_template — строку шаблона.')
        try:
            profile_counter(encoded).count_payload({"messages": [{"role": "user", "content": "Profile validation"}], "reasoning": {"enabled": False}})
        except Exception:
            invalid('Некорректный tokenizer.json или шаблон чата. Поддерживаемые переменные: messages, tools, add_generation_prompt, enable_thinking.')


@lru_cache(maxsize=4)
def profile_counter(encoded):
    from .context_tokens import NativeCounter
    profile = json.loads(encoded)
    class UploadedCounter(NativeCounter):
        strategy = 'uploaded_profile_v1'
        def __init__(self):
            self.tokenizer = Tokenizer.from_str(json.dumps(profile['tokenizer']))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined)
            env.filters['tojson'] = lambda value, **kwargs: json.dumps(value, ensure_ascii=False, **kwargs)
            self.template = env.from_string(profile['chat_template'])
        def count_payload(self, payload):
            rendered = self.template.render(messages=payload['messages'], tools=payload.get('tools', []), add_generation_prompt=True, enable_thinking=payload.get('reasoning', {}).get('enabled', True))
            # Include tool definitions even if the supplied chat template omits them.
            extra = json.dumps(payload['tools'], ensure_ascii=False) if payload.get('tools') else ''
            return self.count_text(rendered + extra)
    return UploadedCounter()
