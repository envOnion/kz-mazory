"""Shared selection of the CRM endpoint for admin display and worker requests."""

from urllib.parse import urlsplit
from django.conf import settings


def effective_webhook_url(config):
    return config.webhook_url.strip() or settings.BITRIX_WEBHOOK_URL.strip()


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
