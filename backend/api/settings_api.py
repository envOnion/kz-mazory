"""Settings for the messenger shell; reuse model validation and durable commands."""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError as ModelValidationError
from django.db import models, transaction
from django.shortcuts import get_object_or_404
from django.views.decorators.debug import sensitive_variables
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .mfa import (attempt_allowed, confirm_session, matching_counter,
                  require_privileged_mfa, verified, INVALID_CODE, ATTEMPT_LIMIT)
from .models import (AISettings, AdminMFA, AuditEvent, BitrixSettings, Team,
                     WhatsAppConfig, WhatsAppHistoryJob, WhatsAppHistoryRun)
from .providers import ProviderUnavailable, checked_base_url

SAFE_AI_FIELDS = (
    'name', 'thread_routing_mode', 'laya_match_threshold', 'laya_match_margin', 'routing_policy_version',
    'thread_extract_idle_seconds', 'thread_extract_max_wait_seconds',
    'thread_extract_batch_size', 'analysis_input_token_limit',
    'analysis_target_message_limit', 'analysis_output_token_limit',
    'autonomous_daily_token_limit', 'autonomous_max_in_flight',
    'autonomous_enabled', 'autonomous_crm_enabled', 'autonomous_policy_version', 'autonomous_context_messages',
    'autonomous_input_tokens', 'chat_api_format', 'chat_provider_url',
    'chat_model_name', 'chat_temperature', 'context_window_tokens',
    'max_completion_tokens', 'context_safety_tokens', 'daily_request_limit',
    'minute_request_limit', 'daily_budget_usd', 'is_active',
    'embedding_provider_url', 'embedding_model_name', 'embedding_dimension',
    'tokenizer_id', 'tokenizer_revision', 'system_prompt_worker', 'system_prompt_assistant',
)
SAFE_BITRIX_FIELDS = (
    'name', 'is_active', 'crm_matching_enabled', 'auto_create_deals',
    'auto_import_deals', 'hourly_sync_enabled', 'auto_create_tasks',
    'sync_timeline_comments', 'deal_category_id', 'deal_object_field_code',
    'default_assigned_by_id',
)
SAFE_WHATSAPP_FIELDS = ('name', 'team', 'group_jid', 'session_name', 'is_active', 'thread_routing_enabled')
SAFE_TEAM_FIELDS = ('name','is_active','history_complete_from','stalled_days','low_margin_percent')
SAFE_HISTORY_FIELDS = (
    'enabled', 'only_new', 'new_message_poll_seconds', 'interval_minutes',
    'analyze_after_import', 'page_size', 'initial_wait_seconds', 'poll_seconds',
    'stable_scans_required',
)
# Model verbose_names without translations get an explicit Russian UI label.
LABELS = {
    'name': 'Название', 'team': 'Команда', 'group_jid': 'Идентификатор группы WhatsApp',
    'session_name': 'Сессия WAHA', 'is_active': 'Активно',
    'thread_extract_idle_seconds': 'Ожидание пакета, секунды',
    'thread_extract_max_wait_seconds': 'Максимальное ожидание, секунды',
    'thread_extract_batch_size': 'Размер пакета', 'autonomous_daily_token_limit': 'Токенов в сутки для автономной обработки',
    'autonomous_max_in_flight': 'Одновременных обработок', 'autonomous_enabled': 'Автономный анализ',
    'autonomous_crm_enabled': 'Автономная запись в CRM', 'autonomous_context_messages': 'Сообщений контекста',
    'autonomous_input_tokens': 'Вход автономной обработки, токены',
    'chat_api_format': 'Формат API диалога', 'chat_provider_url': 'Базовый URL диалога',
    'chat_model_name': 'Модель диалога', 'chat_temperature': 'Температура',
    'embedding_provider_url': 'Базовый URL векторизации', 'embedding_model_name': 'Модель векторизации',
    'history_complete_from':'Начало полной истории, ГГГГ-ММ-ДД', 'stalled_days':'Дней без движения до предупреждения',
    'low_margin_percent':'Порог низкой маржи, %',
    'routing_policy_version':'Версия политики распределения по темам',
    'autonomous_policy_version':'Версия политики автономной обработки',
}


def audit(request, action, cfg, fields):
    AuditEvent.objects.create(actor=request.user, target_id=cfg.pk,
        target_type=type(cfg).__name__, action=action, before_after={'fields': sorted(fields)})


def require_integration(request, model=WhatsAppConfig, *, write=False):
    if not access.integration_allowed(request.user):
        raise PermissionDenied('Недостаточно прав для настройки интеграций.')
    model_name = model._meta.model_name
    can_change = request.user.has_perm(f'api.change_{model_name}')
    if not can_change and (write or not request.user.has_perm(f'api.view_{model_name}')):
        raise PermissionDenied('Нет права на эту конфигурацию.')
    require_privileged_mfa(request)


def serializer_for(model, fields):
    class SettingsSerializer(serializers.ModelSerializer):
        class Meta:
            pass
    SettingsSerializer.Meta.model = model
    SettingsSerializer.Meta.fields = ('id', *fields)
    return SettingsSerializer


def schema_for(model, fields):
    result = []
    for name in fields:
        field = model._meta.get_field(name)
        kind = ('boolean' if isinstance(field, models.BooleanField) else
                'integer' if isinstance(field, (models.IntegerField, models.ForeignKey)) else
                'number' if isinstance(field, models.FloatField) else
                'decimal' if isinstance(field, models.DecimalField) else
                'text' if isinstance(field, models.TextField) else 'string')
        result.append({'name': name, 'label': LABELS.get(name, str(field.verbose_name)),
            'type': kind, 'help': str(field.help_text),
            'choices': [{'value': v, 'label': label} for v, label in field.choices] if field.choices else [],
            'max_length': field.max_length})
    return result


def update_model(request, cfg, fields):
    if not isinstance(request.data, dict) or not request.data:
        raise ValidationError({'error': 'Передайте объект с изменяемыми полями.'})
    if set(request.data) - set(fields):
        raise ValidationError({'error': 'Запрос содержит недоступные поля.'})
    for name, value in request.data.items():
        field = cfg._meta.get_field(name)
        if value is None and field.null:
            continue
        if isinstance(field, models.BooleanField) and type(value) is not bool:
            raise ValidationError({name: 'Укажите логическое значение.'})
        if isinstance(field, (models.IntegerField, models.ForeignKey)) and type(value) is not int:
            raise ValidationError({name: 'Укажите целое число.'})
        if isinstance(field, models.FloatField) and (
            type(value) not in (int, float) or not math.isfinite(value)
        ):
            raise ValidationError({name: 'Укажите конечное число.'})
    schema = serializer_for(type(cfg), fields)(cfg, data=request.data, partial=True)
    schema.is_valid(raise_exception=True)
    values = schema.validated_data
    if isinstance(cfg, Team) and not 0 <= values.get('low_margin_percent',cfg.low_margin_percent) <= 100:
        raise ValidationError({'low_margin_percent':'Укажите процент от 0 до 100.'})
    if isinstance(cfg, AISettings):
        for purpose in ('chat', 'embedding'):
            key = f'{purpose}_provider_url'
            if key in values:
                try:
                    values[key] = checked_base_url(values[key])
                except ProviderUnavailable:
                    raise ValidationError({key: 'Нужен HTTPS URL без логина, параметров и нестандартного порта.'}) from None
            host_changed = key in values and urlsplit(values[key]).hostname != urlsplit(getattr(cfg, key)).hostname
            format_changed = purpose == 'chat' and values.get('chat_api_format', cfg.chat_api_format) != cfg.chat_api_format
            if (host_changed or format_changed) and getattr(cfg, f'has_{purpose}_api_key'):
                raise ValidationError({key: 'Перед сменой хоста или протокола явно удалите сохранённый ключ.'})
        if not 0 <= values.get('chat_temperature', cfg.chat_temperature) <= 2:
            raise ValidationError({'chat_temperature': 'Допустимо от 0 до 2.'})
        if values.get('embedding_dimension', cfg.embedding_dimension) < 1:
            raise ValidationError({'embedding_dimension': 'Размерность должна быть положительной.'})
    if isinstance(cfg, BitrixSettings):
        code = values.get('deal_object_field_code', cfg.deal_object_field_code).strip().upper()
        if code and not re.fullmatch(r'UF_CRM_[A-Z0-9_]+', code):
            raise ValidationError({'deal_object_field_code': 'Укажите код UF_CRM_*.'})
        if 'deal_object_field_code' in values:
            values['deal_object_field_code'] = code
    for name, value in values.items():
        setattr(cfg, name, value)
    try:
        cfg.full_clean(exclude=['webhook_url','inbound_token'] if isinstance(cfg, BitrixSettings) else None)
    except ModelValidationError as exc:
        raise ValidationError(getattr(exc, 'message_dict', {'error': exc.messages})) from None
    cfg.save()
    audit(request, 'settings.update', cfg, values)
    return cfg


class SettingsAccessView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        entry = AdminMFA.objects.filter(user=request.user, confirmed_at__isnull=False).first()
        return Response({'can_integrate': access.integration_allowed(request.user),
            'can_administer_access': request.user.is_superuser,
            'can_ai': request.user.has_perm('api.view_aisettings') or request.user.has_perm('api.change_aisettings'),
            'can_create_ai': access.integration_allowed(request.user) and request.user.has_perm('api.add_aisettings'),
            'can_create_whatsapp': access.integration_allowed(request.user) and request.user.has_perm('api.add_whatsappconfig'),
            'can_bitrix': request.user.has_perm('api.view_bitrixsettings') or request.user.has_perm('api.change_bitrixsettings'),
            'can_create_bitrix': access.integration_allowed(request.user) and request.user.has_perm('api.add_bitrixsettings'),
            'mfa_required': bool(entry and not verified(request, entry)),
            'mfa_enabled': bool(entry),
            'teams': list(Team.objects.filter(is_active=True).filter(
                models.Q() if access.integration_allowed(request.user) else models.Q(pk__in=access.team_ids(request.user))
            ).values('id', 'name'))})

    @sensitive_variables()
    def post(self, request):
        code = request.data.get('code') if isinstance(request.data, dict) else None
        if not isinstance(code, str):
            raise ValidationError({'error': INVALID_CODE})
        with transaction.atomic():
            entry = get_object_or_404(AdminMFA.objects.select_for_update(), user=request.user, confirmed_at__isnull=False)
            if not attempt_allowed(request.user.pk):
                raise ValidationError({'error': ATTEMPT_LIMIT})
            counter = matching_counter(entry, code)
            if counter is None:
                raise ValidationError({'error': INVALID_CODE})
            entry.last_counter = counter
            entry.save(update_fields=['last_counter'])
            confirm_session(request, entry)
        return Response({'verified': True})


class SettingsAIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_integration(request, AISettings, write=request.method != 'GET')
        cfgs = AISettings.objects.order_by('-is_active', 'id')
        rows = serializer_for(AISettings, SAFE_AI_FIELDS)(cfgs, many=True).data
        for row, cfg in zip(rows, cfgs):
            row['keys'] = {'chat': cfg.has_chat_api_key, 'embedding': cfg.has_embedding_api_key}
            row['updated_at'] = cfg.updated_at.isoformat()
        defaults = dict(serializer_for(AISettings, SAFE_AI_FIELDS)(AISettings(is_active=False)).data)
        defaults['id'] = 0
        return Response({'items': rows, 'fields': schema_for(AISettings, SAFE_AI_FIELDS), 'defaults': defaults, 'can_change': request.user.has_perm('api.change_' + cfgs.model._meta.model_name)})

    @transaction.atomic
    def post(self, request):
        require_integration(request, AISettings, write=True)
        if not request.user.has_perm('api.add_aisettings'):
            raise PermissionDenied('Нет права создавать конфигурации ИИ.')
        cfg = update_model(request, AISettings(), SAFE_AI_FIELDS)
        return Response({'id': cfg.pk}, status=201)


class SettingsAIDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, pk):
        require_integration(request, AISettings, write=request.method != 'GET')
        cfg = get_object_or_404(AISettings.objects.select_for_update(), pk=pk)
        update_model(request, cfg, SAFE_AI_FIELDS)
        return Response({'id': cfg.id, 'updated_at': cfg.updated_at.isoformat()})

    @transaction.atomic
    @sensitive_variables()
    def post(self, request, pk):
        from .ai_credentials import CredentialError
        require_integration(request, AISettings, write=request.method != 'GET')
        cfg = get_object_or_404(AISettings.objects.select_for_update(), pk=pk)
        data = request.data
        if not isinstance(data, dict) or set(data) - {'key', 'value', 'clear'}:
            raise ValidationError({'error': 'Неверные параметры ключа.'})
        key = data.get('key')
        if key not in ('chat', 'embedding') or ('clear' in data and type(data['clear']) is not bool):
            raise ValidationError({'error': 'Укажите тип ключа и действие.'})
        if data.get('clear') is True:
            if 'value' in data:
                raise ValidationError({'error': 'Выберите замену или удаление ключа.'})
            getattr(cfg, f'clear_{key}_api_key')()
        else:
            value = data.get('value')
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise ValidationError({'error': 'Введите непустой ключ до 4096 символов.'})
            try:
                getattr(cfg, f'set_{key}_api_key')(value.strip())
            except CredentialError:
                raise ValidationError({'error': 'Шифрование ключей недоступно.'}) from None
        cfg.save(update_fields=[f'{key}_api_key', f'{key}_api_key_encrypted', 'updated_at'])
        audit(request, 'settings.ai.key', cfg, [f'{key}_api_key'])
        return Response({'id': cfg.id, 'chat': cfg.has_chat_api_key, 'embedding': cfg.has_embedding_api_key})


class SettingsWhatsAppView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_integration(request)
        # Integration administrators already manage all configs in the existing WAHA API.
        cfgs = WhatsAppConfig.objects.select_related('team').order_by('id')
        rows = serializer_for(WhatsAppConfig, SAFE_WHATSAPP_FIELDS)(cfgs, many=True).data
        from .waha_admin_views import control_state
        for row, cfg in zip(rows, cfgs):
            row.update(status=cfg.status, updated_at=cfg.updated_at.isoformat(), control=control_state(cfg), qr_image=None)
            if cfg.status == 'SCAN_QR_CODE' and cfg.last_qr_code:
                import base64
                import qrcode
                from qrcode.image.svg import SvgPathFillImage
                from qrcode.exceptions import DataOverflowError
                try:
                    svg = qrcode.make(cfg.last_qr_code, image_factory=SvgPathFillImage).to_string()
                    row['qr_image'] = 'data:image/svg+xml;base64,' + base64.b64encode(svg).decode()
                except DataOverflowError:
                    row['qr_error'] = 'Сохранённый QR некорректен. Запросите новый QR.'
        defaults = dict(serializer_for(WhatsAppConfig, SAFE_WHATSAPP_FIELDS)(WhatsAppConfig(is_active=False)).data)
        defaults['id'] = 0
        return Response({'items': rows, 'fields': schema_for(WhatsAppConfig, SAFE_WHATSAPP_FIELDS), 'defaults': defaults, 'can_change': request.user.has_perm('api.change_' + cfgs.model._meta.model_name)})

    @transaction.atomic
    def post(self, request):
        require_integration(request, WhatsAppConfig, write=True)
        if not request.user.has_perm('api.add_whatsappconfig'):
            raise PermissionDenied('Нет права создавать источники WhatsApp.')
        cfg = update_model(request, WhatsAppConfig(), SAFE_WHATSAPP_FIELDS)
        return Response({'id': cfg.pk}, status=201)


class SettingsWhatsAppDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, pk):
        require_integration(request)
        cfg = get_object_or_404(WhatsAppConfig.objects.select_for_update(), pk=pk)
        update_model(request, cfg, SAFE_WHATSAPP_FIELDS)
        return Response({'id': cfg.pk})

    def post(self, request, pk):
        from .waha_control import enqueue_control, ACTION_LABELS
        require_integration(request)
        action = request.data.get('action') if isinstance(request.data, dict) else None
        if action not in ACTION_LABELS:
            raise ValidationError({'error': 'Неизвестное действие.'})
        cfg = get_object_or_404(WhatsAppConfig, pk=pk, is_active=True)
        try:
            event = enqueue_control(request.user, cfg, action)
        except ModelValidationError as exc:
            raise ValidationError({'error': exc.messages}) from None
        return Response({'event_id': event.pk, 'state': event.state}, status=202)


class SettingsBitrixView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_integration(request, BitrixSettings, write=request.method != 'GET')
        from .communication import sanitize_body
        cfgs = BitrixSettings.objects.order_by('id')
        rows = serializer_for(BitrixSettings, SAFE_BITRIX_FIELDS)(cfgs, many=True).data
        for row, cfg in zip(rows, cfgs):
            row.update(connection_configured=bool(cfg.webhook_url), inbound_configured=bool(cfg.inbound_token), last_sync_at=cfg.last_sync_at,
                       last_sync_status=sanitize_body(cfg.last_sync_status), last_hourly_sync_at=cfg.last_hourly_sync_at, updated_at=cfg.updated_at)
            from .bitrix_config import autonomous_crm_status, AUTONOMOUS_CRM_DISABLED_REASONS
            ai = AISettings.get_active()
            state = autonomous_crm_status(ai, cfg)
            row['runtime'] = state
            row['disabled_reason'] = AUTONOMOUS_CRM_DISABLED_REASONS.get(state['disabled_reason'], '')
            row['ai_settings_href'] = '/settings?section=ai' if ai.pk and (
                request.user.has_perm('api.view_aisettings') or request.user.has_perm('api.change_aisettings')) else None
        defaults = dict(serializer_for(BitrixSettings, SAFE_BITRIX_FIELDS)(BitrixSettings(
            is_active=False, auto_create_deals=False, auto_create_tasks=False,
            auto_import_deals=False, hourly_sync_enabled=False, sync_timeline_comments=False)).data)
        defaults['id'] = 0
        return Response({'items': rows, 'defaults': defaults, 'fields': schema_for(BitrixSettings, SAFE_BITRIX_FIELDS), 'can_change': request.user.has_perm('api.change_bitrixsettings')})

    @transaction.atomic
    def patch(self, request):
        require_integration(request, BitrixSettings, write=request.method != 'GET')
        cfg = get_object_or_404(BitrixSettings.objects.select_for_update())
        update_model(request, cfg, SAFE_BITRIX_FIELDS)
        return Response({'id': cfg.pk})

    @transaction.atomic
    @sensitive_variables()
    def post(self, request):
        require_integration(request, BitrixSettings, write=True)
        data = request.data
        if isinstance(data,dict) and 'key' not in data:
            if not request.user.has_perm('api.add_bitrixsettings'):
                raise PermissionDenied('Нет права создавать конфигурацию Bitrix24.')
            cfg = update_model(request, BitrixSettings(is_active=False, auto_create_deals=False,
                auto_create_tasks=False, auto_import_deals=False, hourly_sync_enabled=False,
                sync_timeline_comments=False), SAFE_BITRIX_FIELDS)
            return Response({'id':cfg.pk},status=201)
        if not isinstance(data, dict) or set(data) - {'key', 'value', 'clear'}:
            raise ValidationError({'error': 'Неверные параметры подключения.'})
        key = data.get('key')
        if key not in ('webhook_url', 'inbound_token') or ('clear' in data and type(data['clear']) is not bool):
            raise ValidationError({'error': 'Укажите тип подключения и действие.'})
        cfg = get_object_or_404(BitrixSettings.objects.select_for_update())
        if data.get('clear') is True:
            if 'value' in data:
                raise ValidationError({'error': 'Выберите замену или удаление.'})
            value = ''
        else:
            value = data.get('value')
            if not isinstance(value, str) or not value.strip() or len(value) > cfg._meta.get_field(key).max_length:
                raise ValidationError({'error': 'Введите непустое значение допустимой длины.'})
            value = value.strip()
            if key == 'webhook_url':
                from .bitrix_config import checked_bitrix_webhook_base
                try:
                    value = checked_bitrix_webhook_base(value)
                except ProviderUnavailable:
                    raise ValidationError({'error': 'Нужен HTTPS REST webhook вида https://портал/rest/ID/ключ/.'}) from None
                if len(value) > 255:
                    raise ValidationError({'error': 'Адрес не должен превышать 255 символов.'})
        setattr(cfg, key, value)
        cfg.save(update_fields=[key, 'updated_at'])
        audit(request, 'settings.bitrix.connection', cfg, [key])
        return Response({'id': cfg.pk, 'connection_configured': bool(cfg.webhook_url), 'inbound_configured': bool(cfg.inbound_token)})


class SettingsHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        require_integration(request, WhatsAppHistoryJob, write=request.method != 'GET')
        cfg = get_object_or_404(WhatsAppConfig, pk=pk)
        job = WhatsAppHistoryJob.objects.filter(config=cfg).first()
        from .history_jobs import progress
        runs = list(job.runs.order_by('-id')[:30]) if job else []
        return Response({'job': serializer_for(WhatsAppHistoryJob, SAFE_HISTORY_FIELDS)(job).data if job else None,
            'fields': schema_for(WhatsAppHistoryJob, SAFE_HISTORY_FIELDS),
            'runs': [{'id': run.pk, 'state': run.state, 'progress': progress(run)} for run in runs]})

    @transaction.atomic
    def patch(self, request, pk):
        require_integration(request, WhatsAppHistoryJob, write=request.method != 'GET')
        cfg = get_object_or_404(WhatsAppConfig.objects.select_for_update(), pk=pk)
        job, _ = WhatsAppHistoryJob.objects.get_or_create(config=cfg)
        update_model(request, job, SAFE_HISTORY_FIELDS)
        return Response({'id': job.pk})

    @transaction.atomic
    def post(self, request, pk):
        require_integration(request, WhatsAppHistoryJob, write=request.method != 'GET')
        if not isinstance(request.data, dict):
            raise ValidationError({'error':'Передайте объект параметров.'})
        cfg = get_object_or_404(WhatsAppConfig, pk=pk)
        job, _ = WhatsAppHistoryJob.objects.get_or_create(config=cfg)
        from .history_jobs import start_job, control_run, reanalyze_saved
        try:
            if 'file' in request.FILES:
                from .admin_forms import WhatsAppExportForm
                from .whatsapp_exports import start_file
                form = WhatsAppExportForm(request.data, request.FILES)
                if not form.is_valid():
                    raise ValidationError(form.errors)
                d = form.cleaned_data
                run = start_file(job.pk, d['file'], d['timezone'], d['date_order'], request.user, str(d['request_key']))
            elif request.data.get('action') == 'start':
                run = start_job(job.pk, request.user)
            else:
                run_id = request.data.get('run_id')
                if type(run_id) is not int:
                    raise ValidationError({'error': 'Укажите целочисленный ID запуска.'})
                run = get_object_or_404(WhatsAppHistoryRun, pk=run_id, job=job)
                if request.data.get('action') == 'reanalyze':
                    run = reanalyze_saved(run.pk, request.user)
                elif request.data.get('action') in ('pause', 'resume', 'cancel'):
                    run = control_run(run.pk, request.data['action'], request.user)
                else:
                    raise ValidationError({'error': 'Неизвестное действие.'})
        except (ModelValidationError, ProviderUnavailable) as exc:
            raise ValidationError({'error': exc.messages if isinstance(exc, ModelValidationError) else 'Источник недоступен.'}) from None
        return Response({'id': run.pk, 'state': run.state}, status=202)


class SettingsDiagnosticsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_integration(request)
        # Audit may contain other users' business data; only a superuser sees it.
        if not request.user.is_superuser:
            raise PermissionDenied('Диагностика аудита доступна администратору.')
        from .settings_diagnostics import diagnostics
        return Response(diagnostics(request))


class AccessGrantInput(serializers.Serializer):
    kind = serializers.ChoiceField(choices=['team','chat','client'])
    id = serializers.IntegerField(min_value=1, required=False)
    name = serializers.CharField(max_length=255, required=False)
    user_id = serializers.IntegerField(min_value=1, required=False)
    config_id = serializers.IntegerField(min_value=1, required=False)
    project_id = serializers.IntegerField(min_value=1, required=False)
    enabled = serializers.BooleanField(default=True)
    status = serializers.ChoiceField(choices=['invited','active','revoked'], default='active')


class SettingsGrantsView(APIView):
    permission_classes = [IsAuthenticated]

    def require_admin(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied('Управление доступом разрешено администратору.')
        require_privileged_mfa(request)

    def get(self, request):
        self.require_admin(request)
        from django.contrib.auth import get_user_model
        from .models import ChatAccess, ClientProjectAccess, Project
        return Response({
            'teams': serializer_for(Team,SAFE_TEAM_FIELDS)(Team.objects.order_by('id'),many=True).data,
            'team_fields':schema_for(Team,SAFE_TEAM_FIELDS),
            'users': list(get_user_model().objects.order_by('id').values('id','username','first_name','last_name','is_active')),
            'configs': list(WhatsAppConfig.objects.order_by('id').values('id','name')),
            'projects': list(Project.objects.order_by('id').values('id','name')),
            'chats': list(ChatAccess.objects.order_by('id').values('id','user_id','config_id','is_active')),
            'clients': list(ClientProjectAccess.objects.order_by('id').values('id','user_id','project_id','status')),
        })

    @transaction.atomic
    def patch(self, request):
        self.require_admin(request)
        ident=request.query_params.get('team','')
        if not ident.isdecimal():
            raise ValidationError({'error':'Укажите целочисленный ID команды.'})
        team=get_object_or_404(Team.objects.select_for_update(),pk=int(ident))
        team.rules_version += 1
        update_model(request,team,SAFE_TEAM_FIELDS)
        return Response({'id':team.pk,'rules_version':team.rules_version})

    @transaction.atomic
    def post(self, request):
        self.require_admin(request)
        from django.contrib.auth import get_user_model
        from .models import ChatAccess, ClientProjectAccess, Project
        schema = AccessGrantInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        if values['kind'] == 'team':
            if not values.get('name'):
                raise ValidationError({'name':'Укажите название команды.'})
            obj = get_object_or_404(Team.objects.select_for_update(),pk=values['id']) if values.get('id') else Team()
            obj.name, obj.is_active = values['name'], values['enabled']
            if obj.pk:
                obj.rules_version += 1
            obj.full_clean(); obj.save()
        else:
            user = get_object_or_404(get_user_model(), pk=values.get('user_id'))
            if values['kind'] == 'chat':
                config = get_object_or_404(WhatsAppConfig,pk=values.get('config_id'))
                obj, _ = ChatAccess.objects.update_or_create(user=user,config=config,defaults={'is_active':values['enabled']})
            else:
                project = get_object_or_404(Project,pk=values.get('project_id'))
                obj, _ = ClientProjectAccess.objects.update_or_create(user=user,project=project,defaults={'status':values['status']})
        audit(request,'settings.access.update',obj,values)
        return Response({'id':obj.pk})
