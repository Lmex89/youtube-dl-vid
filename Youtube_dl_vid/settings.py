from pathlib import Path

from .logging_config import logger

logger.info("Django application started")

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "5!4a_zj5c!-(vv)^8taqortb*)vuw3&w7w&4=!dmd97xz&4udu"

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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "youtube_project",
        "USER": "lmex",
        "PASSWORD": "postgres",
        "HOST": "db",
        "PORT": "5432",
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
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
        "django_file": {
            "class": "logging.FileHandler",
            "filename": "/var/log/gunicorn/django.log",
            "formatter": "json",
        },
        "requests_file": {
            "class": "logging.FileHandler",
            "filename": "/var/log/gunicorn/django_requests.log",
            "formatter": "json",
        },
        "db_file": {
            "class": "logging.FileHandler",
            "filename": "/var/log/gunicorn/django_db.log",
            "formatter": "json",
        },
        "slow_queries_file": {
            "class": "logging.FileHandler",
            "filename": "/var/log/gunicorn/django_slow_queries.log",
            "formatter": "json",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "django_file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "requests_file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console", "db_file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console", "django_file"],
            "level": "WARNING",
            "propagate": False,
        },
        "videos": {
            "handlers": ["console", "django_file"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console", "django_file"],
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

CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False

REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'Youtube_dl_vid.exceptions.custom_exception_handler',
}
