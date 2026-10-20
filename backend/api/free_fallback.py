"""Server-owned, immutable, free-only recovery routing; no catalogue requests."""
import copy
import re
from types import SimpleNamespace
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .providers import ProviderUnavailable

KEY = 'free_fallback_policy'
VERSION = 'free-fallback-v1'
LOCAL_VERSION = 'free-fallback-v2'
INTENT_KEY = 'free_local_budget_intent'
REQUEST_ACTION = 'analysis_local_budget_requested'
ACTIVATION_ACTION = 'analysis_local_budget_activated'
MODEL = 'openrouter/free'
FIELDS = {'version', 'model', 'context_window', 'max_output_tokens',
          'response_format', 'structured_outputs', 'input_price', 'output_price'}


def _snapshot(policy):
    local = isinstance(policy, dict) and policy.get('version') == LOCAL_VERSION
    if (not isinstance(policy, dict) or set(policy) != FIELDS | ({'local_input_budget'} if local else set())
            or policy['version'] not in (VERSION, LOCAL_VERSION) or not isinstance(policy['model'], str)
            or len(policy['model']) > 128
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*:free', policy['model'])
            or any(type(policy[k]) is not int or policy[k] <= 0 for k in ('context_window', 'max_output_tokens'))
            or policy['response_format'] is not True or policy['structured_outputs'] is not True):
        raise ProviderUnavailable('context_configuration_changed')
    if local and (type(policy['local_input_budget']) is not int
                  or not 0 < policy['local_input_budget'] <= 500000
                  or policy['context_window'] > 1000000):
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
    if metadata[KEY]['version'] == LOCAL_VERSION:
        result._free_global_input_budget = getattr(cfg, '_free_global_input_budget', cfg.analysis_input_token_limit)
    return result


def local_runtime(cfg):
    """Apply only after the caller validates the original global snapshot."""
    policy = getattr(cfg, KEY, None)
    if not isinstance(policy, dict) or policy.get('version') != LOCAL_VERSION:
        return cfg
    _cfg(policy, cfg)
    base = getattr(cfg, '_free_global_input_budget', None)
    if (type(base) is not int or base <= 0
            or type(cfg.context_safety_tokens) is not int or cfg.context_safety_tokens < 0):
        raise ProviderUnavailable('context_configuration_changed')
    result = copy.copy(cfg)
    # Expand only the original global baseline. Every other adaptive cap is
    # authoritative, including a cap between the global and local budgets.
    result.analysis_input_token_limit = (policy['local_input_budget']
        if cfg.analysis_input_token_limit == base else min(cfg.analysis_input_token_limit, policy['local_input_budget']))
    result.context_window_tokens = policy['context_window']
    if policy['local_input_budget'] + result.max_completion_tokens + result.context_safety_tokens > result.context_window_tokens:
        raise ProviderUnavailable('context_configuration_changed')
    return result


def local_window_valid(cfg):
    policy = getattr(cfg, KEY, None)
    if not isinstance(policy, dict) or policy.get('version') != LOCAL_VERSION:
        return False
    expected = local_runtime(cfg)
    return (cfg.context_window_tokens == expected.context_window_tokens
            and cfg.analysis_input_token_limit == expected.analysis_input_token_limit)


def check_page(cfg, page):
    policy = getattr(cfg, KEY, None)
    if isinstance(policy, dict) and policy.get('version') == LOCAL_VERSION and page and page.get(KEY) != policy:
        raise ProviderUnavailable('context_snapshot_mismatch')


def runtime_metadata(cfg):
    policy = getattr(cfg, KEY, None)
    if isinstance(policy, dict) and policy.get('version') == LOCAL_VERSION:
        return {'free_local_global_input_budget':cfg._free_global_input_budget}
    return {}


def validate_intent(intent, audit, event, raw, trace, following, state, cfg):
    """Validate a server-audited boundary transition without changing objects."""
    from .processing_attempts import ANALYSIS_CONFIG_FIELDS
    try:
        metadata, saved = trace.context_metadata, audit.before_after
        prefix, stages = saved['initial_stage_trace_ids'], state['stage_trace_ids']
        policy = intent['policy']
        if (set(intent) != {'audit_id', 'request_key', 'policy'}
                or type(intent['audit_id']) is not int or intent['audit_id'] != audit.pk
                or not isinstance(intent['request_key'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', intent['request_key'])
                or audit.action != REQUEST_ACTION or audit.target_type != 'OutboxEvent' or audit.target_id != event.pk
                or type(audit.actor_id) is not int or not audit.actor_id
                or event.event_type != 'extract_message' or event.payload['raw_id'] != raw.pk
                or event.payload['trace_id'] != trace.pk or trace.raw_message_id != raw.pk
                or following.raw_message_id != raw.pk or raw.processed or state.get('page')
                or metadata.get('request_state') != 'segment_validated'
                or metadata.get('full_history_policy') != 'thread-context-v2'
                or saved['event_id'] != event.pk or saved['raw_id'] != raw.pk
                or saved['actor_id'] != audit.actor_id or saved['request_key'] != intent['request_key']
                or saved['free_fallback_policy'] != policy
                or not isinstance(prefix, list) or not prefix or any(type(pk) is not int or pk <= 0 for pk in prefix)
                or not isinstance(stages, list) or not stages or any(type(pk) is not int or pk <= 0 for pk in stages)
                or len(prefix) != len(set(prefix)) or stages[:len(prefix)] != prefix
                or stages[-1] != trace.pk or len(stages) != len(set(stages))
                or not re.fullmatch(r'[a-f0-9]{64}', saved['source_hash'])
                or saved['source_hash'] != state['source_hash'] or saved['source_hash'] != metadata['full_history']['source_hash']
                or type(saved['snapshot_max_id']) is not int or saved['snapshot_max_id'] != metadata['snapshot_max_id']
                or following.context_metadata.get('request_state') != 'not_sent'
                or any(key in following.context_metadata for key in ('request_envelope', 'provider_result', 'segment_result',
                    'main_response_cache', 'focused_requests', 'focused_extraction', 'promise_identity', 'promise_reconciliation'))):
            raise ProviderUnavailable('context_snapshot_mismatch')
        _snapshot(policy)
        previous = metadata[KEY]
        if (policy['version'] != LOCAL_VERSION or previous.get('version') != VERSION
                or following.context_metadata.get(KEY) != previous
                or {**{key:value for key,value in policy.items() if key != 'local_input_budget'}, 'version':VERSION} != previous):
            raise ProviderUnavailable('context_configuration_changed')
        limits = following.context_metadata['analysis_limits']
        if any(name not in limits or limits[name] != getattr(cfg, name) for name in ANALYSIS_CONFIG_FIELDS):
            raise ProviderUnavailable('context_configuration_changed')
        candidate = with_policy(cfg, {**metadata, KEY:policy})
        candidate.analysis_input_token_limit = limits['analysis_input_token_limit']
        candidate.max_completion_tokens = min(candidate.max_completion_tokens, limits['analysis_output_token_limit'])
        candidate = local_runtime(candidate)
        from .context_tokens import context_runtime
        _, endpoint = context_runtime(candidate)
        models(candidate, endpoint)
        return copy.deepcopy(policy)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProviderUnavailable('context_snapshot_mismatch') from None


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


def check_frozen(payload, metadata, model_version, cfg=None):
    if KEY not in metadata:
        return
    try:
        model = _snapshot(metadata[KEY])
        if (metadata.get('full_history_policy') != 'thread-context-v2'
                or model_version != MODEL or payload.get('model') != MODEL or payload.get('models') != [model]
                or metadata.get('api_format') != 'openai_compatible'
                or not _official(metadata.get('effective_provider_url', metadata.get('provider_url')))):
            raise ProviderUnavailable('context_snapshot_mismatch')
        if metadata[KEY]['version'] == LOCAL_VERSION:
            if cfg is None:
                # Pure cache/checkpoint readers have no active cfg. Reconstruct
                # only the saved global snapshot; the pipeline checks live drift.
                cfg = SimpleNamespace(**metadata['analysis_limits'])
                cfg.max_completion_tokens = min(cfg.max_completion_tokens, cfg.analysis_output_token_limit)
                cfg = with_policy(cfg, metadata)
                cfg._free_global_input_budget = metadata['free_local_global_input_budget']
                cfg.analysis_input_token_limit = metadata['analysis_limits']['analysis_input_token_limit']
                cfg = local_runtime(cfg)
            if (getattr(cfg, KEY, None) != metadata[KEY] or not local_window_valid(cfg)
                    or type(metadata.get('free_local_global_input_budget')) is not int
                    or metadata['free_local_global_input_budget'] != cfg._free_global_input_budget):
                raise ProviderUnavailable('context_snapshot_mismatch')
            expected = {'window_tokens':cfg.context_window_tokens, 'user_input_token_limit':cfg.analysis_input_token_limit,
                        'completion_reserve_tokens':cfg.max_completion_tokens, 'safety_tokens':cfg.context_safety_tokens,
                        'effective_input_budget':min(cfg.analysis_input_token_limit,
                            cfg.context_window_tokens-cfg.max_completion_tokens-cfg.context_safety_tokens)}
            if (any(type(metadata.get(key)) is not int or metadata[key] != value for key,value in expected.items())
                    or payload.get('max_tokens') != cfg.max_completion_tokens):
                raise ProviderUnavailable('context_snapshot_mismatch')
    except (ProviderUnavailable, KeyError, TypeError, AttributeError):
        raise ProviderUnavailable('context_snapshot_mismatch') from None
