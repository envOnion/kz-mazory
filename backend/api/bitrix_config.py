"""Shared selection and validation of the CRM endpoint."""

import re
from urllib.parse import urlsplit


from .providers import ProviderUnavailable, checked_url


BITRIX_WEBHOOK_PATH = re.compile(
    r"/rest/[1-9][0-9]*/[A-Za-z0-9_-]+/?"
)


def effective_webhook_url(config):
    return config.webhook_url.strip()


AUTONOMOUS_CRM_DISABLED_REASONS = {
    "integration_disabled": "Интеграция Bitrix выключена.",
    "webhook_missing": "Не задан Webhook для подключения к Bitrix.",
    "autonomous_crm_disabled": "Автоматическая запись результатов WhatsApp в CRM выключена в настройках ИИ.",
}


def autonomous_crm_status(ai_config, integration):
    """Describe configured delivery permissions without contacting Bitrix."""
    integration_enabled = integration.is_active
    webhook_configured = bool(effective_webhook_url(integration))
    autonomous_write_allowed = ai_config.autonomous_crm_enabled
    if not integration_enabled:
        reason = "integration_disabled"
    elif not webhook_configured:
        reason = "webhook_missing"
    elif not autonomous_write_allowed:
        reason = "autonomous_crm_disabled"
    else:
        reason = None
    return {
        "integration_enabled": integration_enabled,
        "webhook_configured": webhook_configured,
        "autonomous_write_allowed": autonomous_write_allowed,
        "effective_autonomous_write_enabled": reason is None,
        "disabled_reason": reason,
    }


def checked_bitrix_webhook_base(value):
    """Return a canonical webhook base that is safe to append fixed methods to."""
    if not isinstance(value, str) or not value.strip():
        raise ProviderUnavailable("crm_not_configured")
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ProviderUnavailable("bitrix_webhook_invalid") from None
    # checked_url enforces HTTPS, port 443, and no userinfo.
    checked_url(value)
    # urlsplit keeps path parameters in path, so the exact regex rejects them too.
    if (
        any(delimiter in value for delimiter in ("?", "#", ";"))
        or parsed.query
        or parsed.fragment
        or not BITRIX_WEBHOOK_PATH.fullmatch(parsed.path)
    ):
        raise ProviderUnavailable("bitrix_webhook_invalid")
    return value.rstrip("/") + "/"


def masked_webhook_url(config):
    url = effective_webhook_url(config)
    if not url:
        return "Не настроен"
    try:
        parsed = urlsplit(url)
        # Never display path fragments: malformed legacy values can contain secrets too.
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}/rest/•••/•••/"
    except ValueError:
        pass
    return "Адрес задан (скрыт)"
