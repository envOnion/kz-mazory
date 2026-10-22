"""Role-scoped settings and queue-only provider diagnostics."""
import uuid
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .models import AISettings, AsyncOperation
from .settings_api import SAFE_AI_FIELDS, require_integration, update_model, audit

CHAT_FIELDS = tuple(name for name in SAFE_AI_FIELDS if name.startswith(('chat_', 'tokenizer_', 'system_prompt_', 'analysis_', 'entity_')) or name in ('context_window_tokens', 'max_completion_tokens', 'context_safety_tokens'))
EMBEDDING_FIELDS = tuple(name for name in SAFE_AI_FIELDS if name.startswith('embedding_'))
DECISION_FIELDS = tuple(name for name in SAFE_AI_FIELDS if name.startswith('decision_') or name in ('thread_routing_mode', 'laya_match_threshold', 'laya_match_margin', 'routing_policy_version'))
ROLE_FIELDS = {'chat': CHAT_FIELDS, 'embedding': EMBEDDING_FIELDS, 'decision': DECISION_FIELDS,
              'shared': tuple(name for name in SAFE_AI_FIELDS if name not in CHAT_FIELDS + EMBEDDING_FIELDS + DECISION_FIELDS)}


def role_fields(role):
    if role not in ROLE_FIELDS:
        raise ValidationError({'error': 'Неизвестная роль модели.'})
    return ROLE_FIELDS[role]


class SettingsModelView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, pk, role):
        require_integration(request, AISettings, write=True)
        cfg = get_object_or_404(AISettings.objects.select_for_update(), pk=pk)
        update_model(request, cfg, role_fields(role))
        return Response({'id': cfg.id})


class SettingsModelCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, role):
        require_integration(request, AISettings)
        role_fields(role)
        value = request.query_params.get('operation_id', '')
        if not value.isdecimal() or len(value) > 18:
            raise ValidationError({'error': 'Укажите ID операции.'})
        op = get_object_or_404(AsyncOperation, pk=int(value), requested_by=request.user, operation_type='ai_connection_check', request__config_id=pk, request__role=role)
        if op.expires_at <= timezone.now() or op.access_fingerprint != access.fingerprint(request.user):
            return Response({'id': op.id, 'status': 'failed', 'result': None, 'error_code': 'access_or_lifetime_changed'})
        status = {'succeeded': 'completed'}.get(op.status, op.status)
        return Response({'id': op.id, 'status': status, 'result': op.result if status == 'completed' else None, 'error_code': op.error_code})

    @transaction.atomic
    def post(self, request, pk, role):
        require_integration(request, AISettings, write=True)
        role_fields(role)
        if role == 'shared':
            raise ValidationError({'error': 'Выберите конкретную модель.'})
        cfg = get_object_or_404(AISettings, pk=pk)
        if role == 'decision' and not cfg.decision_enabled:
            raise ValidationError({'error': 'Модель решений отключена. Обработка выполняется чат-моделью.'})
        from .ai_connection_checks import config_fingerprint
        pending = AsyncOperation.objects.filter(requested_by=request.user, operation_type='ai_connection_check', request__config_id=pk, request__role=role, status__in=['queued', 'running'], expires_at__gt=timezone.now()).first()
        if pending:
            return Response({'operation_id': pending.id}, status=202)
        op = AsyncOperation.objects.create(requested_by=request.user, operation_type='ai_connection_check', request={'config_id': pk, 'role': role, 'fingerprint': config_fingerprint(cfg, role)}, idempotency_key=uuid.uuid4().hex, expires_at=timezone.now() + timedelta(minutes=10), access_fingerprint=access.fingerprint(request.user))
        def enqueue():
            from django_q.tasks import async_task
            try:
                async_task('api.tasks.check_model_connection', op.id, cluster='ai')
            except Exception:
                AsyncOperation.objects.filter(pk=op.id).update(status='failed', error_code='queue_unavailable')
        transaction.on_commit(enqueue)
        audit(request, 'settings.ai.check', cfg, [role])
        return Response({'operation_id': op.id}, status=202)
