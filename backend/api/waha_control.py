"""Durable WAHA commands; only tasks.py contacts the provider."""

import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AuditEvent, OutboxEvent, WhatsAppConfig

ACTION_LABELS = {
    "start": "Запустить сессию",
    "restart": "Перезапустить",
    "stop": "Остановить",
    "logout": "Выйти (Logout)",
    "status": "Обновить статус",
    "qr": "Обновить QR-код",
}
ACTIVE_STATES = ("pending", "enqueued", "processing")


@transaction.atomic
def enqueue_control(user, config, action):
    if action not in ACTION_LABELS:
        raise ValidationError("Неизвестная команда WAHA.")
    # Configurations for different groups can share one WAHA session.
    configs = list(
        WhatsAppConfig.objects.select_for_update()
        .filter(session_name=config.session_name)
        .order_by("id")
    )
    if not any(item.id == config.id and item.is_active for item in configs):
        raise ValidationError("Конфигурация выключена или изменена. Обновите страницу.")
    if OutboxEvent.objects.filter(
        event_type="waha_control",
        payload__config_id__in=[item.id for item in configs],
        state__in=ACTIVE_STATES,
    ).exists():
        raise ValidationError(
            "Для этой сессии уже выполняется команда. Дождитесь результата."
        )
    event = OutboxEvent.objects.create(
        event_type="waha_control",
        deduplication_key=f"waha:{uuid.uuid4().hex}",
        payload={
            "config_id": config.id,
            "session_name": config.session_name,
            "action": action,
        },
    )
    AuditEvent.objects.create(
        actor=user, target_type="WhatsAppConfig", target_id=config.id, action=action
    )
    return event
