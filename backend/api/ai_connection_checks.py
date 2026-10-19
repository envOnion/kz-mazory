"""Background-only connection checks with permission and configuration snapshots."""
import hashlib
import json
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.utils import timezone

from . import access
from .ai_credentials import CredentialError
from .models import AISettings, AsyncOperation
from .providers import ProviderUnavailable, checked_base_url


def config_fingerprint(cfg, role):
    from .ai_settings_api import ROLE_FIELDS
    values = {name: getattr(cfg, name) for name in ROLE_FIELDS[role]}
    values['key'] = getattr(cfg, f'{role}_api_key_encrypted')
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def check_connection(operation_id):
    op = AsyncOperation.objects.filter(pk=operation_id, status='queued', operation_type='ai_connection_check').first()
    if not op or not AsyncOperation.objects.filter(pk=op.id, status='queued').update(status='running'):
        return
    try:
        user = get_user_model().objects.get(pk=op.requested_by_id, is_active=True)
        if op.expires_at <= timezone.now() or access.fingerprint(user) != op.access_fingerprint or not access.integration_allowed(user) or not user.has_perm('api.change_aisettings'):
            raise ProviderUnavailable('access_or_lifetime_changed')
        cfg = AISettings.objects.get(pk=op.request['config_id'])
        role = op.request['role']
        if config_fingerprint(cfg, role) != op.request['fingerprint']:
            raise ProviderUnavailable('configuration_changed')
        from .ai_service import AIService
        base = checked_base_url(getattr(cfg, f'{role}_provider_url')).rstrip('/')
        key = getattr(cfg, f'get_{role}_api_key')()
        if role == 'decision':
            from .decision_models import predictions
            predictions(SimpleNamespace(content='Техническая проверка подключения. Без коммерческих фактов.', sender_name='Mazory'), [{'id': 1, 'version': 1, 'topic': 'Проверка подключения', 'summary': 'Техническая проверка подключения модели.'}], cfg)
        elif role == 'embedding':
            import math
            data = AIService._post(base + '/embeddings', {'model': cfg.embedding_model_name, 'input': 'Проверка подключения Mazory'}, (5, 30), operation='embedding', api_key=key, config=cfg)
            rows = data.get('data') if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ProviderUnavailable('provider_invalid_response')
            vector = rows[0].get('embedding')
            if not isinstance(vector, list) or len(vector) != cfg.embedding_dimension or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
                raise ProviderUnavailable('embedding_dimension_mismatch')
        else:
            from .ai_providers import anthropic_headers
            payload = {'model': cfg.chat_model_name, 'max_tokens': 64, 'messages': [{'role': 'user', 'content': 'Ответь коротко: OK. Это техническая проверка подключения.'}]}
            if cfg.chat_api_format == 'anthropic_messages':
                data = AIService._post(base + '/v1/messages', payload, (5, 60), api_format=cfg.chat_api_format, operation='chat', headers=anthropic_headers(key), config=cfg)
                valid = isinstance(data, dict) and data.get('stop_reason') == 'end_turn' and any(isinstance(row, dict) and row.get('type') == 'text' and row.get('text', '').strip() for row in data.get('content', []))
            else:
                data = AIService._post(base + '/chat/completions', payload, (5, 60), operation='chat', api_key=key, config=cfg)
                choices = data.get('choices') if isinstance(data, dict) else None
                valid = isinstance(choices, list) and bool(choices) and choices[0].get('finish_reason') == 'stop' and isinstance(choices[0].get('message', {}).get('content'), str) and bool(choices[0]['message']['content'].strip())
            if not valid:
                raise ProviderUnavailable('provider_invalid_response')
        current = AISettings.objects.get(pk=cfg.id)
        if config_fingerprint(current, role) != op.request['fingerprint']:
            raise ProviderUnavailable('configuration_changed')
        AsyncOperation.objects.filter(pk=op.id).update(status='succeeded', result={'role': role, 'model': getattr(cfg, f'{role}_model_name'), 'checked_at': timezone.now().isoformat(), 'message': 'Провайдер ответил; формат ответа проверен. Качество модели не оценивалось.'}, error_code='')
    except (ProviderUnavailable, CredentialError) as exc:
        AsyncOperation.objects.filter(pk=op.id).update(status='failed', error_code=str(exc)[:64], result={})
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('AI connection check failed operation=%s error_type=%s', op.id, type(exc).__name__)
        AsyncOperation.objects.filter(pk=op.id).update(status='failed', error_code='connection_check_failed', result={})
