"""
Django settings for mazory_backend project with Unfold Admin Theme.
"""

from pathlib import Path
import os
import sys
from datetime import timedelta
from django.templatetags.static import static

BASE_DIR = Path(__file__).resolve().parent.parent

from django.core.exceptions import ImproperlyConfigured
import secrets

MAZORY_ENV = os.getenv("MAZORY_ENV", "development")
TESTING = "test" in sys.argv or MAZORY_ENV == "test"
DEBUG = MAZORY_ENV == "development" and os.getenv("DEBUG", "1") == "1"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY and TESTING:
    SECRET_KEY = "isolated-test-only-key-never-valid-in-production-12345"
elif not SECRET_KEY and MAZORY_ENV == "development":
    key_path = BASE_DIR / ".dev-secret"
    if not key_path.exists():
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(64))
        except FileExistsError:
            pass
    SECRET_KEY = key_path.read_text().strip()
if (
    len(SECRET_KEY) < 50
    or SECRET_KEY.startswith("django-insecure-")
    or (
        MAZORY_ENV == "production"
        and (
            len(set(SECRET_KEY)) < 16
            or "test-only" in SECRET_KEY
            or "isolated-test" in SECRET_KEY
        )
    )
):
    raise ImproperlyConfigured("Set a random SECRET_KEY with at least 50 characters.")
JWT_SIGNING_KEY = os.getenv("JWT_SIGNING_KEY", SECRET_KEY)
if MAZORY_ENV == "production" and (
    JWT_SIGNING_KEY == SECRET_KEY
    or len(JWT_SIGNING_KEY) < 50
    or len(set(JWT_SIGNING_KEY)) < 16
    or JWT_SIGNING_KEY.startswith("django-insecure-")
):
    raise ImproperlyConfigured("Invalid JWT_SIGNING_KEY")
ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,backend").split(",")
    if h.strip()
]
if MAZORY_ENV == "production" and "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be explicit")
SECURE_SSL_REDIRECT = MAZORY_ENV == "production"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = MAZORY_ENV == "production"
SECURE_HSTS_SECONDS = 31536000 if MAZORY_ENV == "production" else 0
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
AUTH_REFRESH_COOKIE = "mazory_refresh"
AUTH_COOKIE_SECURE = MAZORY_ENV == "production"
OTP_PHONE_HOUR_LIMIT = 5
OTP_PHONE_DAY_LIMIT = 10
OTP_IP_HOUR_LIMIT = 30
MFA_ENCRYPTION_KEY = os.getenv("MFA_ENCRYPTION_KEY", "")
if MAZORY_ENV == "production" and not MFA_ENCRYPTION_KEY:
    raise ImproperlyConfigured("MFA_ENCRYPTION_KEY is required")
AI_CREDENTIAL_ENCRYPTION_KEY = os.getenv("AI_CREDENTIAL_ENCRYPTION_KEY", "")
if AI_CREDENTIAL_ENCRYPTION_KEY:
    from cryptography.fernet import Fernet

    try:
        Fernet(AI_CREDENTIAL_ENCRYPTION_KEY.encode("ascii"))
    except (UnicodeEncodeError, ValueError, TypeError):
        raise ImproperlyConfigured(
            "AI_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key"
        ) from None
# Explicit, temporary rollout escape hatch. A database credential always takes
# precedence, and an invalid ciphertext never falls back to an environment key.
AI_PROVIDER_ENV_FALLBACK = os.getenv("AI_PROVIDER_ENV_FALLBACK", "0") == "1"
# CSRF & Reverse Proxy settings (поддержка localhost, ai.mazory.best и Nginx)
CSRF_TRUSTED_ORIGINS = [
    "http://localhost",
    "http://localhost:8080",
    "http://localhost:8000",
    "http://localhost:5173",
    "http://127.0.0.1",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:5173",
    "https://localhost",
    "https://127.0.0.1",
    "https://ai.mazory.best",
    "http://ai.mazory.best",
]
extra_csrf = os.getenv("CSRF_TRUSTED_ORIGINS", "")
if extra_csrf:
    CSRF_TRUSTED_ORIGINS.extend(
        [origin.strip() for origin in extra_csrf.split(",") if origin.strip()]
    )

# Поддержка заголовков проксирования Nginx
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# Application definition - UNFOLD must be before django.contrib.admin
INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "django_q",
    # Local apps
    "api.apps.ApiConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "api.security.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "api.mfa.AdminMFAMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = [
        "https://ai.mazory.best",
        "http://ai.mazory.best",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ]
    extra_cors = os.getenv("CORS_ALLOWED_ORIGINS", "")
    if extra_cors:
        CORS_ALLOWED_ORIGINS.extend(
            [origin.strip() for origin in extra_cors.split(",") if origin.strip()]
        )
CORS_ALLOW_CREDENTIALS = True

ROOT_URLCONF = "mazory_backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "mazory_backend.wsgi.application"

# Database
POSTGRES_DB = os.getenv("POSTGRES_DB", "mazory_db")
POSTGRES_USER = os.getenv("POSTGRES_USER", "mazory")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

import socket


def _can_resolve(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except Exception:
        return False


POSTGRES_HOST = os.getenv("POSTGRES_HOST")
if not POSTGRES_HOST:
    POSTGRES_HOST = "postgres" if _can_resolve("postgres") else "127.0.0.1"

POSTGRES_PORT = os.getenv("POSTGRES_PORT")
if not POSTGRES_PORT:
    POSTGRES_PORT = "5432" if POSTGRES_HOST == "postgres" else "5434"

if ("test" in sys.argv or os.getenv("USE_SQLITE") == "1") and not os.getenv(
    "FORCE_POSTGRES_TEST"
):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3"
            if os.getenv("USE_SQLITE") == "1"
            else ":memory:",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": POSTGRES_DB,
            "USER": POSTGRES_USER,
            "PASSWORD": POSTGRES_PASSWORD,
            "HOST": POSTGRES_HOST,
            "PORT": POSTGRES_PORT,
        }
    }

# Never fall back to the writer when analytics credentials are absent.
if os.getenv('ANALYTICS_DB_PASSWORD'):
    DATABASES['analytics_readonly'] = {
        **DATABASES['default'],
        'USER': os.getenv('ANALYTICS_DB_USER', 'mazory_analytics'),
        'PASSWORD': os.getenv('ANALYTICS_DB_PASSWORD'),
        'OPTIONS': {'options': '-c default_transaction_read_only=on'},
        'TEST': {'MIRROR': 'default'},
    }

QDRANT_URL = os.getenv("QDRANT_URL")
if not QDRANT_URL:
    QDRANT_URL = (
        "http://qdrant:6333" if _can_resolve("qdrant") else "http://127.0.0.1:6333"
    )

QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "mazory_messages")

# Internationalization
LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Almaty"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/private-media/"
MEDIA_ROOT = BASE_DIR / "media"

# Unfold Admin Settings
UNFOLD = {
    "SITE_TITLE": "Mazory AI Sales Platform",
    "SITE_HEADER": "Mazory AI",
    "SITE_URL": "/",
    "SITE_ICON": {
        "light": "/static/mazory/icon.png",
        "dark": "/static/mazory/icon.png",
    },
    "SITE_LOGO": {
        "light": "/static/mazory/logo.png",
        "dark": "/static/mazory/logo.png",
    },
    "SITE_FAVICONS": [
        {
            "rel": "icon",
            "sizes": "32x32",
            "type": "image/png",
            "href": "/static/mazory/icon.png",
        },
    ],
    "COLORS": {
        "primary": {
            "50": "238 242 255",
            "100": "224 231 255",
            "200": "199 210 254",
            "300": "165 180 252",
            "400": "129 140 248",
            "500": "99 102 241",
            "600": "79 70 229",
            "700": "67 56 202",
            "800": "55 48 163",
            "900": "49 46 129",
            "950": "30 27 75",
        },
    },
    "STYLES": [
        lambda request: static("mazory/css/admin_layout.css"),
        lambda request: static("mazory/css/admin_trace.css"),
    ],
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": [
            {
                "title": "Управление продажами",
                "separator": True,
                "items": [
                    {
                        "title": "Проекты / Сделки",
                        "icon": "work",
                        "link": "/admin/api/project/",
                    },
                    {
                        "title": "Контрагенты / Компании",
                        "icon": "business",
                        "link": "/admin/api/company/",
                    },
                    {
                        "title": "Обязательства и обещания",
                        "icon": "event_available",
                        "link": "/admin/api/commitment/",
                    },
                    {
                        "title": "Финансовые движения",
                        "icon": "payments",
                        "link": "/admin/api/financialrecord/",
                    },
                ],
            },
            {
                "title": "WhatsApp & WAHA",
                "separator": True,
                "items": [
                    {
                        "title": "WAHA Центр управления (QR)",
                        "icon": "qr_code_scanner",
                        "link": "/admin/waha-dashboard/",
                    },
                    {
                        "title": "Настройки WhatsApp (Чат/Группа)",
                        "icon": "chat",
                        "link": "/admin/api/whatsappconfig/",
                    },
                    {
                        "title": "Входящие сообщения",
                        "icon": "forum",
                        "link": "/admin/api/rawmessage/",
                    },
                    {
                        "title": "Трассировка WhatsApp → Bitrix → Итог",
                        "icon": "account_tree",
                        "link": "/admin/api/messageprocessingtrace/",
                    },
                ],
            },
            {
                "title": "AI & Интеграции",
                "separator": True,
                "items": [
                    {
                        "title": "Настройки AI",
                        "icon": "smart_toy",
                        "link": "/admin/api/aisettings/",
                    },
                    {
                        "title": "Интеграция с Bitrix24",
                        "icon": "sync",
                        "link": "/admin/api/bitrixsettings/",
                    },
                    {
                        "title": "Логи изменений сделок Bitrix24",
                        "icon": "history",
                        "link": "/admin/api/bitrixdealchangelog/",
                    },
                    {
                        "title": "Бизнес-события",
                        "icon": "notifications_active",
                        "link": "/admin/api/businessevent/",
                    },
                ],
            },
            {
                "title": "Фоновые задания",
                "separator": True,
                "items": [
                    {
                        "title": "Настройки импорта WhatsApp",
                        "icon": "settings",
                        "link": "/admin/api/whatsapphistoryjob/",
                    },
                    {
                        "title": "Запуски и прогресс",
                        "icon": "play_circle",
                        "link": "/admin/api/whatsapphistoryrun/",
                    },
                    {
                        "title": "Очередь и ошибки",
                        "icon": "pending_actions",
                        "link": "/admin/api/outboxevent/",
                    },
                ],
            },
            {
                "title": "Администрирование",
                "separator": True,
                "items": [
                    {
                        "title": "Пользователи системы",
                        "icon": "people",
                        "link": "/admin/auth/user/",
                    },
                    {
                        "title": "Профили сотрудников",
                        "icon": "badge",
                        "link": "/admin/api/userprofile/",
                    },
                ],
            },
            {
                "title": "Личные настройки",
                "separator": True,
                "items": [
                    {
                        "title": "Безопасность",
                        "icon": "security",
                        "link": "/admin/security/",
                    }
                ],
            },
        ],
    },
}

REDIS_HOST = os.getenv("REDIS_HOST")
if not REDIS_HOST:
    REDIS_HOST = "redis" if _can_resolve("redis") else "127.0.0.1"

REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
WAHA_API_URL = os.getenv("WAHA_API_URL", "http://waha:3000")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"redis://{REDIS_HOST}:{REDIS_PORT}/1",
    }
}

AI_REQUEST_TIMEOUT = int(os.getenv("AI_REQUEST_TIMEOUT", "300"))
AI_WORKER_TIMEOUT = AI_REQUEST_TIMEOUT + 180
AI_TASK_LEASE = AI_WORKER_TIMEOUT + 60

Q_CLUSTER = {
    "name": "mazory_q",
    "workers": 2,
    "max_attempts": 3,
    "ack_failures": True,
    "ALT_CLUSTERS": {
        "delivery": {"workers": 2, "timeout": 30, "retry": 60},
        "ai": {"workers": 2, "timeout": AI_WORKER_TIMEOUT, "retry": AI_TASK_LEASE + 60},
        "history": {
            "workers": 1,
            "timeout": AI_WORKER_TIMEOUT,
            "retry": AI_TASK_LEASE + 60,
        },
        "crm": {"workers": 1, "timeout": 90, "retry": 120},
    },
    "recycle": 500,
    "timeout": AI_WORKER_TIMEOUT,
    "retry": AI_TASK_LEASE + 60,
    "sync": "test" in sys.argv,
    "redis": {
        "host": REDIS_HOST,
        "port": REDIS_PORT,
        "db": 0,
    },
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["api.renderers.ExactJSONRenderer"],
    "EXCEPTION_HANDLER": "api.security.api_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "120/min"},
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "api.authentication.McpTokenAuthentication",
        "api.authentication.SessionJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": JWT_SIGNING_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

BITRIX_INBOUND_TOKEN = os.getenv("BITRIX_INBOUND_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
TRANSCRIPTION_URL = os.getenv("TRANSCRIPTION_URL", "")
OCR_URL = os.getenv("OCR_URL", "")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact": {"()": "api.security.RedactFilter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "filters": ["redact"]}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
if MAZORY_ENV == "production":
    CSRF_TRUSTED_ORIGINS = [
        x for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x
    ]
    CORS_ALLOWED_ORIGINS = [
        x for x in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if x
    ]
    CORS_ALLOW_ALL_ORIGINS = False
if TESTING and os.getenv("TEST_CACHE", "memory") == "memory":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

WAHA_WEBHOOK_SECRET = os.getenv("WAHA_WEBHOOK_SECRET", "")
BITRIX_TEAM_ID = int(os.getenv("BITRIX_TEAM_ID", "0"))

MEDIA_PROVIDER_KEY = os.getenv("MEDIA_PROVIDER_KEY", "")
if TESTING:
    ALLOWED_HOSTS += ["testserver", "backend"]
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if MAZORY_ENV == "production" else None
)

if MAZORY_ENV == "production":
    ALLOWED_HOSTS += ["backend"]
    SECURE_REDIRECT_EXEMPT = [r"^api/whatsapp/webhook/$", r"^api/health/(live|ready)/$"]

# Costs are provider-reported; missing prices remain unknown. Limits bound future calls.
from decimal import Decimal

AI_DAILY_REQUEST_LIMIT = int(os.getenv("AI_DAILY_REQUEST_LIMIT", "1000"))
AI_DAILY_BUDGET_USD = Decimal(os.getenv("AI_DAILY_BUDGET_USD", "20"))

TRUSTED_PROXY_CIDRS = [
    value for value in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",") if value
]

# Exact local-stage -> provider-stage IDs; each CRM pipeline supplies its own map.
import json

try:
    BITRIX_STAGE_MAP = json.loads(os.getenv("BITRIX_STAGE_MAP", "") or "{}")
    if not isinstance(BITRIX_STAGE_MAP, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v
        for k, v in BITRIX_STAGE_MAP.items()
    ):
        raise ValueError()
except (ValueError, TypeError):
    raise ImproperlyConfigured(
        "BITRIX_STAGE_MAP must be a JSON object of stage strings"
    ) from None
