"""Atomic primary selection and ordered chat-provider candidates."""

from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AISettings, AuditEvent

pinned_configuration = ContextVar('pinned_ai_configuration', default=None)


def validate_configuration_pin(cfg):
    pin = pinned_configuration.get()
    if pin is not None:
        from .ai_connection_checks import config_fingerprint
        from .providers import ProviderUnavailable
        if cfg.pk != pin[0] or not cfg.is_primary or config_fingerprint(cfg, 'chat') != pin[1]:
            raise ProviderUnavailable('recovery_configuration_changed')


@transaction.atomic
def delete_configuration(cfg, *, actor):
    """Serialize with admission, preserving completed usage but never a live request."""
    from .models import AsyncOperation, OutboxEvent, ProviderReservation
    from .security import Conflict
    primary = AISettings.objects.select_for_update().filter(is_primary=True, is_active=True).first()
    locked = primary if primary and primary.pk == cfg.pk else AISettings.objects.select_for_update().get(pk=cfg.pk)
    if locked.is_active:
        raise Conflict('Сначала выключите конфигурацию. Для основной сначала выберите другую основную или выключите остальные.')
    if ProviderReservation.objects.filter(config=locked, state='reserved', lease_until__gt=timezone.now()).exists():
        raise Conflict('Конфигурация используется выполняющимся запросом. Дождитесь его завершения.')
    if AsyncOperation.objects.filter(request__config_id=locked.pk, status__in=['queued', 'running'], expires_at__gt=timezone.now()).exists():
        raise Conflict('Для конфигурации ещё выполняется операция. Дождитесь её завершения.')
    recoveries = AsyncOperation.objects.filter(operation_type='error_reanalysis', request__config_id=locked.pk)
    if OutboxEvent.objects.filter(payload__error_recovery_id__in=list(recoveries.values_list('pk', flat=True)), state__in=['pending', 'enqueued', 'processing']).exists():
        raise Conflict('Для конфигурации ещё выполняется повторный анализ. Дождитесь его завершения.')
    AuditEvent.objects.create(actor=actor, target_type='AISettings', target_id=locked.pk,
        action='settings.ai.deleted', before_after={'fields': ['configuration', 'encrypted_credentials']})
    locked.delete()


@transaction.atomic
def save_configuration(cfg, *, actor=None, update_fields=None):
    """Save profile state while keeping the single-primary invariant across writers."""
    primary_id = AISettings.objects.filter(is_primary=True, is_active=True).values_list("pk", flat=True).first()
    if primary_id:
        AISettings.objects.select_for_update().get(pk=primary_id)
        primary_id = AISettings.objects.filter(is_primary=True, is_active=True).values_list("pk", flat=True).first()
        if primary_id:
            AISettings.objects.select_for_update().get(pk=primary_id)
    if cfg.pk and cfg.pk != primary_id:
        AISettings.objects.select_for_update().get(pk=cfg.pk)
    previous = AISettings.objects.filter(pk=cfg.pk).first() if cfg.pk else None
    previous_primary_id = AISettings.objects.filter(is_primary=True, is_active=True).values_list("pk", flat=True).first()
    active = cfg.is_active
    wants_primary = bool(cfg.is_primary)
    active_others = AISettings.objects.filter(is_active=True).exclude(pk=cfg.pk)
    if previous and previous.is_primary and not active:
        if active_others.exists():
            raise ValidationError({"is_active": "Сначала выберите другую основную конфигурацию."})
        cfg.is_primary = False
        wants_primary = False
    if wants_primary and not active:
        raise ValidationError({"is_primary": "Основная конфигурация должна быть активной."})

    if previous and previous.is_primary and not wants_primary and active and active_others.exists():
        raise ValidationError({"is_primary": "Сначала выберите другую основную конфигурацию."})

    should_be_primary = wants_primary or (active and not AISettings.objects.filter(
        is_primary=True, is_active=True
    ).exclude(pk=cfg.pk).exists())
    if should_be_primary:
        AISettings.objects.exclude(pk=cfg.pk).filter(is_primary=True).update(is_primary=False)
        cfg.is_primary = True
    else:
        cfg.is_primary = False

    if update_fields is not None:
        update_fields = set(update_fields) | {"is_primary", "updated_at"}
        cfg.save(update_fields=update_fields)
    else:
        cfg.save()
    next_primary_id = AISettings.objects.filter(is_primary=True, is_active=True).values_list("pk", flat=True).first()
    if previous_primary_id != next_primary_id:
        AuditEvent.objects.create(
            actor=actor, target_id=cfg.pk, target_type="AISettings",
            action="ai.primary.changed",
            before_after={"previous_config_id": previous_primary_id,
                          "current_config_id": next_primary_id},
        )
    return cfg


def chat_candidates(primary=None):
    """Return the primary followed by active backups in stable ID order."""
    primary = primary or AISettings.get_active()
    if not primary.pk or not primary.is_active:
        return []
    validate_configuration_pin(primary)
    if pinned_configuration.get() is not None:
        return [primary]
    backups = AISettings.objects.filter(is_active=True).exclude(pk=primary.pk).order_by("pk")
    return [primary, *backups]
