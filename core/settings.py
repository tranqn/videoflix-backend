"""
Django settings for the Videoflix core project.

Values are read from the environment (see .env.template). The blocks required by
the course Docker setup (PostgreSQL, Redis cache, RQ queues, WhiteNoise, static
and media paths) are kept as specified by the material README.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from the local .env file (see .env.template).
load_dotenv(BASE_DIR / '.env')

# True while running the test suite; used to swap in self-contained backends
# (sqlite/locmem/sync RQ) at the end of this file. Set either by ``manage.py
# test`` (``sys.argv``) or by ``core.settings_test``, which exports
# DJANGO_TESTING=1 before importing this module (for pytest-django, which
# never populates ``sys.argv`` with ``'test'``).
TESTING = 'test' in sys.argv or os.environ.get('DJANGO_TESTING') == '1'


# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

# SECURITY WARNING: keep the secret key used in production secret! The insecure
# fallback is only tolerated in local development (DEBUG=True).
SECRET_KEY = os.environ.get('SECRET_KEY', '')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-only-key-change-me'
    else:
        raise ImproperlyConfigured('SECRET_KEY must be set when DEBUG is False.')

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if origin
]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_rq',
    'auth_app',
    'video_app',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

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

WSGI_APPLICATION = 'core.wsgi.application'


# Database (PostgreSQL) — configured from the environment.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'videoflix_db'),
        'USER': os.environ.get('DB_USER', 'videoflix_user'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'supersecretpassword'),
        'HOST': os.environ.get('DB_HOST', 'db'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}


# Redis cache (django-redis) — also used to cache the video list.
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': os.environ.get('REDIS_LOCATION', 'redis://redis:6379/1'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
        'KEY_PREFIX': 'videoflix',
    }
}


# Background tasks (django-rq). The entrypoint runs `rqworker default`, so heavy
# jobs (HLS transcoding, e-mail delivery) are enqueued on the 'default' queue.
RQ_QUEUES: dict[str, dict[str, Any]] = {
    'default': {
        'HOST': os.environ.get('REDIS_HOST', 'redis'),
        'PORT': os.environ.get('REDIS_PORT', 6379),
        'DB': os.environ.get('REDIS_DB', 0),
        'DEFAULT_TIMEOUT': 3600,
        'REDIS_CLIENT_KWARGS': {},
    },
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static & media files. WhiteNoise serves the collected static assets; uploaded
# videos and generated HLS output live under MEDIA_ROOT.
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'static'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Django REST Framework: authenticate via JWT stored in HttpOnly cookies.
#
# Throttling: ScopedRateThrottle rather than Anon/User defaults or bespoke
# classes — the three endpoints that need limits want three *different* rates,
# and a scope is one attribute on the view plus one line here, with no new code
# to test. Views without a `throttle_scope` are unaffected.
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ('auth_app.api.authentication.CookieJWTAuthentication',),
    'DEFAULT_PERMISSION_CLASSES': ('rest_framework.permissions.IsAuthenticated',),
    'DEFAULT_THROTTLE_CLASSES': ('rest_framework.throttling.ScopedRateThrottle',),
    'DEFAULT_THROTTLE_RATES': {
        'login': '10/min',
        'register': '5/hour',
        'password_reset': '3/hour',
    },
    # Anonymous callers are bucketed by IP. With NUM_PROXIES unset, DRF trusts
    # the *whole* X-Forwarded-For header, which the client controls — an
    # attacker would get a fresh bucket per forged header and the limits above
    # would be decorative. `1` = exactly one trusted reverse proxy (Caddy, see
    # compose.prod.yml), so DRF reads the last XFF entry, the one Caddy itself
    # appended. Raise this if you ever put a CDN in front of Caddy; with no
    # proxy at all (local runserver) it falls back to REMOTE_ADDR.
    'NUM_PROXIES': int(os.environ.get('NUM_PROXIES', '1')),
}

# simplejwt: access token outlives the frontend's 20-minute refresh interval so
# long video streams never 401 mid-playback. Refresh tokens rotate + blacklist.
#
# Known tradeoff: logout blacklists the refresh token and clears both cookies,
# but a *copy* of the access token taken before logout stays valid for the rest
# of its 60-minute lifetime — simplejwt does not blacklist access tokens. This
# value is held at 60 minutes on purpose: the frontend only renews on a fixed
# 20-minute timer that runs on the video-list page, so a shorter lifetime would
# expire tokens with nothing to renew them and break playback mid-stream. Drop
# it (5-15 minutes) as soon as the client refreshes on a 401 instead.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# HttpOnly auth cookies. 'secure' only over HTTPS, so disabled in local DEBUG.
AUTH_COOKIE_SECURE = not DEBUG

# SECURITY: this line is the CSRF defence for the whole API, not a preference.
# DRF's APIView.as_view() is csrf_exempt, and CsrfViewMiddleware only enforces
# for SessionAuthentication — we authenticate from a cookie-borne JWT, so no
# CSRF token is ever checked on any endpoint. What stops evil.com from POSTing
# to /api/ with the victim's cookies attached is solely SameSite=Lax.
# Switching this to 'None' (the usual fix when the frontend moves to a
# different site) silently makes every state-changing endpoint forgeable, so
# that change MUST come together with real CSRF tokens or an Origin check.
# test_csrf_posture.py asserts this value so the dependency fails loudly.
AUTH_COOKIE_SAMESITE = 'Lax'


# CORS: the frontend is served from a different port (cross-origin) and sends
# credentials, so explicit origins + credentials are required.
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = [
    origin
    for origin in os.environ.get(
        'CORS_ALLOWED_ORIGINS',
        'http://127.0.0.1:5500,http://localhost:5500',
    ).split(',')
    if origin
]


# E-mail (SMTP) — activation and password-reset messages.
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.example.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'False') == 'True'
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'noreply@videoflix.local'
)

# Base URL of the frontend; activation / reset links in e-mails point here.
FRONTEND_BASE_URL = os.environ.get('FRONTEND_BASE_URL', 'http://127.0.0.1:5500')


# Production security hardening. Active when DEBUG=False, assuming a
# TLS-terminating reverse proxy that forwards X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', str(not DEBUG)) == 'True'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0


# Test runner: use self-contained backends so the suite needs no external
# services (PostgreSQL/Redis/SMTP) and RQ jobs execute synchronously.
if TESTING:
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
    CACHES['default'] = {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
    EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    for queue_config in RQ_QUEUES.values():
        queue_config['ASYNC'] = False
