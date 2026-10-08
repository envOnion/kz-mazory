"""Isolated local e2e runtime. Never selectable without a fresh temporary directory."""

import os
from pathlib import Path
from urllib.parse import urlsplit

if os.getenv("MAZORY_ENV") != "test" or os.getenv("MAZORY_E2E_DOCKER") != "1" or not Path('/.dockerenv').exists():
    raise RuntimeError("E2E runs only in the isolated Docker Compose stack")
from mazory_backend.settings import *  # noqa: F403

E2E_DIR = Path(os.environ["MAZORY_E2E_DIR"]).resolve()
if not (E2E_DIR / "isolated-e2e.marker").exists():
    raise RuntimeError("Missing isolated database marker")
# Writer and read-only analytical credentials are configured by compose.e2e.yml.
# Production OperationContext reads the real PostgreSQL alias without a fallback.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
Q_CLUSTER = {
    "name": "local_e2e",
    "workers": 1,
    "orm": "default",
    "sync": False,
    "timeout": 90,
    "retry": 120,
    "poll": 0.2,
    "ack_failures": True,
    "max_attempts": 1,
}
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "backend", "nginx"]
BITRIX_TEAM_ID = 1
WAHA_WEBHOOK_SECRET = "isolated-local-webhook-secret"
WAHA_API_URL = "https://localhost"
WAHA_API_KEY = "isolated-local-waha-key"
SECURE_SSL_REDIRECT = AUTH_COOKIE_SECURE = SESSION_COOKIE_SECURE = (
    CSRF_COOKIE_SECURE
) = False
DEBUG = False
AI_DAILY_REQUEST_LIMIT = 10000
# Serial browser scenarios share one fixture user and poll Q2 faster than a person.
# Production rate limits remain in mazory_backend.settings.
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"user": "1000/min"}}
MEDIA_ROOT = E2E_DIR / "media"
QDRANT_URL = "https://localhost"

# Retain the production URL validation (HTTPS, port 443). Only the
# local test transport maps the validated loopback URL to an unprivileged port.
# All non-loopback provider calls fail closed; no external account is reachable.
import requests

_original_request = requests.sessions.Session.request


def _local_transport(self, method, url, **kwargs):
    parsed = urlsplit(url)
    if (
        parsed.hostname != "localhost"
        or parsed.scheme != "https"
        or parsed.port not in (None, 443)
    ):
        raise RuntimeError("Non-local provider call refused by isolated e2e runtime")
    port = os.environ["MAZORY_E2E_PROVIDER_PORT"]
    host = os.environ["MAZORY_E2E_PROVIDER_HOST"]
    if host != "provider":
        raise RuntimeError("Only the Docker fixture provider is permitted")
    url = url.replace("https://localhost", f"https://{host}:{port}", 1)
    return _original_request(self, method, url, **kwargs)


requests.sessions.Session.request = _local_transport
ROOT_URLCONF = "e2e.urls"
