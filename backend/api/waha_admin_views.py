import base64

import qrcode
from qrcode.exceptions import DataOverflowError
from qrcode.image.svg import SvgPathFillImage
from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import OutboxEvent, WhatsAppConfig
from .access import integration_allowed
from .waha_control import ACTION_LABELS, ACTIVE_STATES, enqueue_control
from .whatsapp_views import ConfigForm

STATE_LABELS = {
    "pending": "Команда в очереди",
    "enqueued": "Команда передана воркеру",
    "processing": "Команда выполняется",
    "done": "Команда выполнена",
    "failed": "Команда завершилась ошибкой",
    "unknown": "Результат неизвестен. Обновите статус перед повторной командой.",
}
ERROR_LABELS = {
    "waha_http_401": "WAHA отклонила ключ доступа. Проверьте настройки сервера.",
    "waha_http_403": "WAHA запретила команду. Проверьте права ключа доступа.",
    "waha_http_404": "Сессия не найдена в WAHA. Проверьте её имя и настройки сервера.",
    "waha_http_422": "WAHA отклонила команду для текущего состояния сессии.",
    "waha_not_configured": "На сервере не настроен ключ WAHA.",
    "waha_outcome_unknown": "Ответ WAHA не получен полностью. Команда могла выполниться и автоматически не повторяется.",
    "provider_timeout": "WAHA не ответила вовремя. Обновите статус.",
    "worker_interrupted": "Воркер был прерван. Обновите статус перед повторной командой.",
    "waha_config_changed": "Конфигурация изменена. Обновите страницу.",
    "ConnectionError": "Не удалось подключиться к WAHA. Проверьте доступность сервера.",
    "waha_groups_not_connected": "Для получения групп подключите WhatsApp и обновите статус.",
    "waha_invalid_groups": "WAHA вернула некорректный список групп. Предыдущий снимок сохранён.",
    "waha_repeated_groups": "WAHA повторяет страницу групп. Неполный список не сохранён; повторите обновление.",
}


def selected_config(data, *, required=False):
    if not required and "config_id" not in data:
        return WhatsAppConfig.objects.filter(is_active=True).first(), "", 200
    form = ConfigForm(data)
    if not form.is_valid():
        return None, "Укажите положительный целочисленный ID конфигурации.", 400
    config = WhatsAppConfig.objects.filter(
        pk=form.cleaned_data["config_id"], is_active=True
    ).first()
    if not config:
        return None, "Конфигурация не найдена или выключена.", 404
    return config, "", 200


def control_state(config):
    return control_states([config]).get(config.pk, _empty_control_state()) if config else _empty_control_state()


def _empty_control_state():
    return {"busy": False, "state": "", "action": "", "message": "", "error": "", "error_code": ""}


def control_states(configs):
    """Batch WAHA state by session while keeping active commands highest priority."""
    configs = list(configs)
    if not configs:
        return {}
    session_names = {config.session_name for config in configs}
    all_configs = list(WhatsAppConfig.objects.filter(session_name__in=session_names)
                       .values_list("id", "session_name"))
    config_sessions = {config_id: session for config_id, session in all_configs}
    config_ids = list(config_sessions)
    if not config_ids:
        return {config.pk: _empty_control_state() for config in configs}

    def latest_rows(*, active_only=False):
        events = OutboxEvent.objects.filter(event_type="waha_control", payload__config_id__in=config_ids)
        if active_only:
            events = events.filter(state__in=ACTIVE_STATES)
        return events.annotate(
            _config_id=F("payload__config_id"),
            _action=F("payload__action"),
            _rank=Window(
                expression=RowNumber(),
                partition_by=[F("payload__config_id")],
                order_by=F("id").desc(),
            ),
        ).filter(_rank=1).values("id", "state", "error_code", "_config_id", "_action")

    latest_by_session = {}
    for row in latest_rows():
        session = config_sessions.get(row["_config_id"])
        if session and (session not in latest_by_session or row["id"] > latest_by_session[session]["id"]):
            latest_by_session[session] = row
    active_by_session = {}
    for row in latest_rows(active_only=True):
        session = config_sessions.get(row["_config_id"])
        if session and (session not in active_by_session or row["id"] > active_by_session[session]["id"]):
            active_by_session[session] = row

    result = {}
    for config in configs:
        event = active_by_session.get(config.session_name) or latest_by_session.get(config.session_name)
        result[config.pk] = {
            "busy": bool(event and event["state"] in ACTIVE_STATES),
            "state": event["state"] if event else "",
            "action": ACTION_LABELS.get(event["_action"], "Команда WAHA") if event else "",
            "message": STATE_LABELS.get(event["state"], event["state"]) if event else "",
            "error": ERROR_LABELS.get(
                event["error_code"],
                "WAHA не подтвердила выполнение. Проверьте доступность сервиса и обновите статус.",
            ) if event and event["error_code"] else "",
            "error_code": event["error_code"] if event else "",
        }
    return result


def dashboard(request, config, error="", status_code=200):
    state = control_state(config)
    group_snapshot = None
    if config and config.status == "WORKING":
        saved = config.snapshot.get("waha_groups") or {}
        account_id = (config.snapshot.get("waha_me") or {}).get("id")
        if (
            saved.get("session_name") == config.session_name
            and account_id
            and saved.get("account_id") == account_id
        ):
            group_snapshot = saved
    groups = group_snapshot["items"] if group_snapshot else []
    selected_group = next((g for g in groups if g["id"] == config.group_jid), None)
    qr_image = None
    if (
        config
        and config.status == "SCAN_QR_CODE"
        and config.last_qr_code
        and not state["busy"]
    ):
        try:
            svg = qrcode.make(
                config.last_qr_code, image_factory=SvgPathFillImage
            ).to_string()
            qr_image = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
        except DataOverflowError:
            error = "Сохранённый QR-код некорректен. Запросите новый QR-код."
    return render(
        request,
        "admin/waha_dashboard.html",
        {
            **admin.site.each_context(request),
            "title": "WAHA Центр управления WhatsApp",
            "config": config,
            "configs": WhatsAppConfig.objects.filter(is_active=True).order_by("id"),
            "actions": [
                (key, label) for key, label in ACTION_LABELS.items() if key != "groups"
            ],
            "operation": state,
            "error": error,
            "qr_image": qr_image,
            "groups": groups,
            "groups_loaded": group_snapshot is not None,
            "groups_updated_at": parse_datetime(group_snapshot["updated_at"])
            if group_snapshot
            else None,
            "selected_group": selected_group,
            "me": config.snapshot.get("waha_me")
            if config and config.status == "WORKING"
            else None,
        },
        status=status_code,
    )


@never_cache
@staff_member_required
@require_GET
def waha_dashboard_view(request):
    if not integration_allowed(request.user):
        raise PermissionDenied()
    config, error, status_code = selected_config(request.GET)
    return dashboard(request, config, error, status_code)


@never_cache
@staff_member_required
@require_GET
def waha_state_view(request):
    if not integration_allowed(request.user):
        raise PermissionDenied()
    config, error, status_code = selected_config(request.GET, required=True)
    return JsonResponse(
        {"error": error} if error else control_state(config), status=status_code
    )


@never_cache
@staff_member_required
@require_POST
def waha_action_view(request, action):
    if not integration_allowed(request.user):
        raise PermissionDenied()
    config, error, status_code = selected_config(request.POST, required=True)
    if error:
        return dashboard(request, config, error, status_code)
    try:
        enqueue_control(request.user, config, action)
    except ValidationError as exc:
        return dashboard(request, config, exc.messages[0], 400)
    messages.info(
        request, "Команда поставлена в очередь. Ниже появится результат выполнения."
    )
    return redirect(f"{reverse('admin:waha_dashboard')}?config_id={config.id}")
