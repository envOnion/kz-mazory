"""
Django settings for mazory_backend project with Unfold Admin Theme.
"""

from pathlib import Path
import os
import sys
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-f7u)rp4lokiv@hgsdma!e@cb*g=dp!z2i)f94)07=nqs^2!ons')

DEBUG = os.getenv('DEBUG', '1') == '1'

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '*').split(',') if os.getenv('ALLOWED_HOSTS') else ['*']
if 'ai.mazory.best' not in ALLOWED_HOSTS and '*' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('ai.mazory.best')

# CSRF & Reverse Proxy settings (поддержка localhost, ai.mazory.best и Nginx)
CSRF_TRUSTED_ORIGINS = [
    'http://localhost',
    'http://localhost:8080',
    'http://localhost:8000',
    'http://localhost:5173',
    'http://127.0.0.1',
    'http://127.0.0.1:8080',
    'http://127.0.0.1:8000',
    'http://127.0.0.1:5173',
    'https://localhost',
    'https://127.0.0.1',
    'https://ai.mazory.best',
    'http://ai.mazory.best',
]
extra_csrf = os.getenv('CSRF_TRUSTED_ORIGINS', '')
if extra_csrf:
    CSRF_TRUSTED_ORIGINS.extend([origin.strip() for origin in extra_csrf.split(',') if origin.strip()])

# Поддержка заголовков проксирования Nginx
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# Application definition - UNFOLD must be before django.contrib.admin
INSTALLED_APPS = [
    'unfold',
    'unfold.contrib.filters',
    'unfold.contrib.forms',
    'unfold.contrib.inlines',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_q',
    # Local apps
    'api.apps.ApiConfig',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

ROOT_URLCONF = 'mazory_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'mazory_backend.wsgi.application'

# Database
POSTGRES_DB = os.getenv('POSTGRES_DB', 'mazory_db')
POSTGRES_USER = os.getenv('POSTGRES_USER', 'mazory')
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'mazory2026')

import socket

def _can_resolve(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except Exception:
        return False

POSTGRES_HOST = os.getenv('POSTGRES_HOST')
if not POSTGRES_HOST:
    POSTGRES_HOST = 'postgres' if _can_resolve('postgres') else '127.0.0.1'

POSTGRES_PORT = os.getenv('POSTGRES_PORT')
if not POSTGRES_PORT:
    POSTGRES_PORT = '5432' if POSTGRES_HOST == 'postgres' else '5434'

if ('test' in sys.argv or os.getenv('USE_SQLITE') == '1') and not os.getenv('FORCE_POSTGRES_TEST'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3' if os.getenv('USE_SQLITE') == '1' else ':memory:',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': POSTGRES_DB,
            'USER': POSTGRES_USER,
            'PASSWORD': POSTGRES_PASSWORD,
            'HOST': POSTGRES_HOST,
            'PORT': POSTGRES_PORT,
        }
    }

QDRANT_URL = os.getenv('QDRANT_URL')
if not QDRANT_URL:
    QDRANT_URL = 'http://qdrant:6333' if _can_resolve('qdrant') else 'http://127.0.0.1:6333'

QDRANT_COLLECTION = os.getenv('QDRANT_COLLECTION', 'mazory_messages')

# Internationalization
LANGUAGE_CODE = 'ru'
TIME_ZONE = 'Asia/Almaty'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

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
                ],
            },
            {
                "title": "AI & Интеграции",
                "separator": True,
                "items": [
                    {
                        "title": "Модели AI (Чат и Embeddings)",
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
        ],
    },
}

REDIS_HOST = os.getenv('REDIS_HOST')
if not REDIS_HOST:
    REDIS_HOST = 'redis' if _can_resolve('redis') else '127.0.0.1'

REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
WAHA_API_URL = os.getenv('WAHA_API_URL', 'http://waha:3000')
WAHA_API_KEY = os.getenv('WAHA_API_KEY', 'mazory-waha-key-2026')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': f'redis://{REDIS_HOST}:{REDIS_PORT}/1',
    }
}

Q_CLUSTER = {
    'name': 'mazory_q',
    'workers': 2,
    'recycle': 500,
    'timeout': 60,
    'retry': 120,
    'sync': 'test' in sys.argv,
    'redis': {
        'host': REDIS_HOST,
        'port': REDIS_PORT,
        'db': 0,
    }
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': False,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
}
