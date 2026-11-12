import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .logging_config import logger

logger.info("Django application started")

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY environment variable is required")

DEBUG = False

ALLOWED_HOSTS = ["*"]

CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]
CORS_ORIGIN_ALLOW_ALL = True
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "videos",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "Youtube_dl_vid.middleware.APILoggingMiddleware",
    "Youtube_dl_vid.db_logging_middleware.SlowQueryLoggingMiddleware",
    "django_ratelimit.middleware.RatelimitMiddleware",
]

ROOT_URLCONF = "Youtube_dl_vid.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "Youtube_dl_vid.wsgi.application"

DB_PASSWORD = os.environ.get("DB_PASSWORD")
if not DB_PASSWORD:
    raise ImproperlyConfigured("DB_PASSWORD environment variable is required")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", ""),
        "USER": os.environ.get("DB_USER", ""),
        "PASSWORD": DB_PASSWORD,
        "HOST": os.environ.get("DB_HOST", "db"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "intercept": {
            "()": "Youtube_dl_vid.logging_config.InterceptHandler",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["intercept"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["intercept"],
            "level": "INFO",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["intercept"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["intercept"],
            "level": "WARNING",
            "propagate": False,
        },
        "videos": {
            "handlers": ["intercept"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["intercept"],
        "level": "INFO",
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Mexico_City"
USE_TZ = True
USE_I18N = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

if DEBUG:
    STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOAD_LOGS_DIR = BASE_DIR / "logs"

FFMPEG_BINARY = "ffmpeg"
FFPROBE_BINARY = "ffprobe"
MEDIA_PROCESS_TIMEOUT = 300  # seconds
WHATSAPP_MEDIA_MAX_BYTES = 16 * 1024 * 1024

CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False

REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'Youtube_dl_vid.exceptions.custom_exception_handler',
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "ratelimit": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
}

RATELIMIT_USE_CACHE = "ratelimit"
RATELIMIT_VIEW = "videos.views.ratelimited_error"
