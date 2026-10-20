import math
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

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


PROVIDER_ERROR_TYPES = frozenset({
    "context_length_exceeded", "max_tokens_exceeded", "token_limit_exceeded", "string_too_long",
    "authentication", "permission_denied", "payment_required", "rate_limit_exceeded",
    "provider_overloaded", "provider_unavailable", "invalid_request", "invalid_prompt", "not_found",
    "precondition_failed", "payload_too_large", "unprocessable", "content_policy_violation", "refusal",
    "invalid_image", "image_too_large", "image_too_small", "unsupported_image_format", "image_not_found",
    "image_download_failed", "server", "timeout", "unmapped",
})


def _rejection_diagnostics(detail, http_status, error_location):
    """Keep finite labels only; provider text is inspected, never retained."""
    metadata = detail.get("metadata") if isinstance(detail, dict) else None
    typed = metadata.get("error_type") if isinstance(metadata, dict) else None
    error_type = typed if isinstance(typed, str) and typed in PROVIDER_ERROR_TYPES else "unknown"
    category = "unknown"
    if not isinstance(metadata, dict) or "error_type" not in metadata:
        message = detail.get("message") if isinstance(detail, dict) else None
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        text = "\n".join(value[:4096].lower() for value in (message, raw) if isinstance(value, str))
        for label, terms in (
            ("context_limit", ("context length", "context_length", "context window", "too many tokens", "prompt is too long", "maximum context")),
            ("output_limit", ("max_tokens", "max_completion_tokens", "output token limit", "completion token limit")),
            ("schema", ("json schema", "json_schema", "response_format", "structured output", "schema")),
            ("parameter", ("unsupported parameter", "unknown parameter", "invalid parameter", "unrecognized parameter")),
            ("availability", ("no endpoints found", "no available endpoints", "provider unavailable", "temporarily unavailable", "overloaded", "service unavailable")),
            ("rate_limit", ("rate limit", "rate_limit", "too many requests")),
            ("content_policy", ("content policy", "content_policy", "safety policy", "moderation", "refusal")),
        ):
            if any(term in text for term in terms):
                category = label
                break
    return {
        "provider_error_type": error_type,
        "provider_error_category": category,
        "provider_http_status": http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
        "provider_error_location": error_location if isinstance(error_location, str) and error_location in ("http", "top_level", "choice") else "unknown",
    }


def provider_error(status, *, retry_after=None, detail=None, http_status=None, error_location=None):
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
    provider_type = detail.get("type") if isinstance(detail, dict) else None
    code = {
        401: "provider_authentication_failed",
        402: "provider_insufficient_credits",
        403: "provider_access_denied",
        408: "provider_timeout",
        429: "provider_rate_limited",
        503: "provider_overloaded",
        529: "provider_overloaded",
    }.get(status)
    if code is None:
        code = {
            "authentication_error": "provider_authentication_failed",
            "permission_error": "provider_access_denied",
            "rate_limit_error": "provider_rate_limited",
            "overloaded_error": "provider_overloaded",
            "api_error": "provider_server_error",
            "invalid_request_error": "provider_invalid_request",
        }.get(provider_type)
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
        diagnostics={"provider_error_code": status, "retry_after_seconds": retry_after,
                     **_rejection_diagnostics(detail, http_status, error_location)},
    )


def checked_ai_url(url):
    """Validate an administrator-selected AI endpoint without a host allowlist."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ProviderUnavailable("provider_not_allowed") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ProviderUnavailable("provider_not_allowed")
    return url


def checked_url(url):
    """Validate outgoing URL syntax without restricting destination hosts."""
    return checked_ai_url(url)


def checked_base_url(url):
    """Validate and normalize a provider base before appending fixed routes."""
    if not isinstance(url, str) or not url:
        raise ProviderUnavailable("ai_not_configured")
    try:
        parsed = urlparse(url)
    except ValueError:
        raise ProviderUnavailable("provider_not_allowed") from None
    # urlparse cannot distinguish an absent delimiter from an explicitly empty
    # one (for example ``...?`` or ``https://@host``). Reject the raw syntax so
    # appending our fixed route cannot silently turn it into a query/fragment.
    if any(delimiter in url for delimiter in (";", "?", "#")):
        raise ProviderUnavailable("provider_not_allowed")
    normalized = url.rstrip("/")
    checked_ai_url(normalized)
    return normalized
