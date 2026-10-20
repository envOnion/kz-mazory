"""Server-owned, immutable, free-only recovery routing; no catalogue requests."""
import copy
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .providers import ProviderUnavailable

KEY = 'free_fallback_policy'
VERSION = 'free-fallback-v1'
MODEL = 'openrouter/free'
FIELDS = {'version', 'model', 'context_window', 'max_output_tokens',
          'response_format', 'structured_outputs', 'input_price', 'output_price'}


def _snapshot(policy):
    if (not isinstance(policy, dict) or set(policy) != FIELDS
            or policy['version'] != VERSION or not isinstance(policy['model'], str)
            or len(policy['model']) > 128
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*:free', policy['model'])
            or any(type(policy[k]) is not int or policy[k] <= 0 for k in ('context_window', 'max_output_tokens'))
            or policy['response_format'] is not True or policy['structured_outputs'] is not True):
        raise ProviderUnavailable('context_configuration_changed')
    for key in ('input_price', 'output_price'):
        value = policy[key]
        try:
            valid = (isinstance(value, str) and 0 < len(value) <= 64 and value == value.strip()
                     and Decimal(value).is_finite() and Decimal(value) == 0)
        except InvalidOperation:
            valid = False
        if not valid:
            raise ProviderUnavailable('context_configuration_changed')
    return policy['model']


def _cfg(policy, cfg):
    model = _snapshot(policy)
    if (cfg.chat_model_name != MODEL or getattr(cfg, 'chat_api_format', None) != 'openai_compatible'
            or not _official(getattr(cfg, 'chat_provider_url', None))
            or any(type(getattr(cfg, key, None)) is not int or getattr(cfg, key) <= 0
                   for key in ('analysis_input_token_limit', 'max_completion_tokens'))
            or policy['context_window'] < cfg.analysis_input_token_limit + cfg.max_completion_tokens
            or policy['max_output_tokens'] < cfg.max_completion_tokens):
        raise ProviderUnavailable('context_configuration_changed')
    return model


def _official(url):
    try:
        if (not isinstance(url, str) or any(ord(char) < 33 for char in url)
                or any(char in url for char in (';', '?', '#'))):
            return False
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == 'openrouter.ai'
                and parsed.port in (None, 443) and parsed.path.rstrip('/') == '/api/v1'
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError, AttributeError):
        return False


def with_policy(cfg, metadata):
    if KEY not in metadata:
        return cfg
    if metadata.get('full_history_policy') != 'thread-context-v2':
        raise ProviderUnavailable('context_configuration_changed')
    _cfg(metadata[KEY], cfg)
    result = copy.copy(cfg)
    setattr(result, KEY, copy.deepcopy(metadata[KEY]))
    return result


def models(cfg, endpoint):
    if not hasattr(cfg, KEY):
        return None
    model = _cfg(getattr(cfg, KEY), cfg)
    if (endpoint.get('transport') != 'openrouter_free'
            or endpoint.get('api_format') != 'openai_compatible'
            or not _official(endpoint.get('effective_provider_url'))):
        raise ProviderUnavailable('context_configuration_changed')
    return [model]


def policy_metadata(metadata):
    return {KEY: copy.deepcopy(metadata[KEY])} if KEY in metadata else {}


def check_frozen(payload, metadata, model_version):
    if KEY not in metadata:
        return
    try:
        model = _snapshot(metadata[KEY])
        if (metadata.get('full_history_policy') != 'thread-context-v2'
                or model_version != MODEL or payload.get('model') != MODEL or payload.get('models') != [model]
                or metadata.get('api_format') != 'openai_compatible'
                or not _official(metadata.get('effective_provider_url', metadata.get('provider_url')))):
            raise ProviderUnavailable('context_snapshot_mismatch')
    except ProviderUnavailable:
        raise ProviderUnavailable('context_snapshot_mismatch') from None
