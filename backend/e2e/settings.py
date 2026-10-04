"""Isolated local e2e runtime. Never selectable without a fresh temporary directory."""

import os
from pathlib import Path
from urllib.parse import urlsplit

if os.getenv("MAZORY_ENV") != "test" or not os.getenv("MAZORY_E2E_DIR"):
    raise RuntimeError("E2E settings require the isolated runner")
from mazory_backend.settings import *  # noqa: F403

E2E_DIR = Path(os.environ["MAZORY_E2E_DIR"]).resolve()
if not (E2E_DIR / "isolated-e2e.marker").exists():
    raise RuntimeError("Missing isolated database marker")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": E2E_DIR / "e2e.sqlite3",
        "OPTIONS": {
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}
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
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
PROVIDER_ALLOWED_HOSTS = ["localhost"]
BITRIX_TEAM_ID = 1
WAHA_WEBHOOK_SECRET = "isolated-local-webhook-secret"
SECURE_SSL_REDIRECT = AUTH_COOKIE_SECURE = SESSION_COOKIE_SECURE = (
    CSRF_COOKIE_SECURE
) = False
DEBUG = False
AI_DAILY_REQUEST_LIMIT = 10000
MEDIA_ROOT = E2E_DIR / "media"
QDRANT_URL = "https://localhost"

# Retain the production URL validation (HTTPS, allowlist, port 443). Only the
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
    url = url.replace("https://localhost", f"https://localhost:{port}", 1)
    return _original_request(self, method, url, **kwargs)


requests.sessions.Session.request = _local_transport
ROOT_URLCONF = "e2e.urls"
