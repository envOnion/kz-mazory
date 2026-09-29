import math
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone


class ProviderUnavailable(Exception):
    def __init__(self, code, *, diagnostics=None, retry_after=None):
        super().__init__(code)
        self.diagnostics = diagnostics or {}
        self.retry_after = retry_after


def retry_after_seconds(value):
    """Parse the standard header; never retain raw provider headers."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return int(value) if len(value) <= 9 else None
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            return None
        return max(0, math.ceil((moment - timezone.now()).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


def provider_error(status, *, retry_after=None, detail=None):
    """Classify both HTTP failures and provider error envelopes inside HTTP 200."""
    if (
        isinstance(status, str)
        and len(status) == 3
        and status.isascii()
        and status.isdigit()
    ):
        status = int(status)
    if type(status) is not int:
        status = None
    code = {
        401: "provider_authentication_failed",
        402: "provider_insufficient_credits",
        403: "provider_access_denied",
        408: "provider_timeout",
        429: "provider_rate_limited",
        503: "provider_overloaded",
    }.get(status)
    metadata = detail.get("metadata", {}) if isinstance(detail, dict) else {}
    if (
        status == 402
        and retry_after is not None
        and isinstance(metadata, dict)
        and metadata.get("limit_source") == "openrouter_in_flight_budget"
    ):
        code = "provider_in_flight_budget"
    if code is None:
        code = (
            "provider_server_error"
            if type(status) is int and 500 <= status <= 599
            else "provider_invalid_request"
            if type(status) is int and 300 <= status <= 499
            else "provider_response_error"
        )
    return ProviderUnavailable(
        code,
        retry_after=retry_after,
        diagnostics={"provider_error_code": status, "retry_after_seconds": retry_after},
    )


def checked_url(url):
    parsed = urlparse(url)
    if (
        settings.INTEGRATION_TEST_MODE
        and parsed.scheme == "http"
        and parsed.hostname == "test-provider"
        and parsed.port == 9000
    ):
        return url
    if (
        parsed.scheme != "https"
        or parsed.hostname not in settings.PROVIDER_ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ProviderUnavailable("provider_not_allowed")
    return url
