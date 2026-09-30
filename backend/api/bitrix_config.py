"""Shared selection and validation of the CRM endpoint."""

import re
from urllib.parse import urlsplit

from django.conf import settings

from .providers import ProviderUnavailable, checked_url


BITRIX_WEBHOOK_PATH = re.compile(
    r"/rest/[1-9][0-9]*/[A-Za-z0-9_-]+/?"
)


def effective_webhook_url(config):
    return config.webhook_url.strip() or settings.BITRIX_WEBHOOK_URL.strip()


def checked_bitrix_webhook_base(value):
    """Return a canonical webhook base that is safe to append fixed methods to."""
    if not isinstance(value, str) or not value.strip():
        raise ProviderUnavailable("crm_not_configured")
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ProviderUnavailable("bitrix_webhook_invalid") from None
    # checked_url enforces HTTPS, the host allowlist, port 443, and no userinfo.
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
