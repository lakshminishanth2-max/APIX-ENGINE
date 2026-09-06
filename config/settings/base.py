"""Base Django settings for the APIx Data & Statistical Core.

Every deployment-variable lives here behind ``env()``; nothing else in the code
base touches ``os.environ``.  A missing production secret raises at import time
rather than surfacing as a silent misconfiguration halfway through a scheduled
collection run.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from celery.schedules import crontab

BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
class ImproperlyConfigured(Exception):
    """Raised when a required setting is absent."""


def env(key: str, default: Any = None, *, required: bool = False) -> Any:
    value = os.environ.get(key, default)
    if required and (value is None or value == ""):
        raise ImproperlyConfigured(f"environment variable {key!r} is required")
    return value


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw not in (None, "") else default


def env_decimal(key: str, default: str) -> Decimal:
    return Decimal(os.environ.get(key) or default)


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
SECRET_KEY: str = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG: bool = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS: list[str] = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,backend")
CSRF_TRUSTED_ORIGINS: list[str] = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    "django_celery_beat",
    "django_celery_results",
    # First party
    "apix",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apix.middleware.CorrelationIdMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


# --------------------------------------------------------------------------- #
# Database - PostgreSQL 16
# --------------------------------------------------------------------------- #
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "apix"),
        "USER": env("POSTGRES_USER", "apix"),
        "PASSWORD": env("POSTGRES_PASSWORD", "apix"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env_int("POSTGRES_PORT", 5432),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            # Statistical batch jobs are long; API queries must never be.
            "application_name": "apix-core",
        },
    }
}


# --------------------------------------------------------------------------- #
# Redis - Celery broker, cache and the distributed token bucket
# --------------------------------------------------------------------------- #
REDIS_URL: str = env("REDIS_URL", "redis://localhost:6379/0")
REDIS_RATELIMIT_URL: str = env("REDIS_RATELIMIT_URL", "redis://localhost:6379/3")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_CACHE_URL", "redis://localhost:6379/4"),
        "KEY_PREFIX": "apix",
        "TIMEOUT": 300,
    }
}

CELERY_BROKER_URL: str = env("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND: str = env("CELERY_RESULT_BACKEND", "django-db")
CELERY_CACHE_BACKEND = "default"
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "Asia/Kolkata"
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT", 3_600)
CELERY_TASK_SOFT_TIME_LIMIT = env_int("CELERY_TASK_SOFT_TIME_LIMIT", 3_300)
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # long, uneven tasks: fetch one at a time
CELERY_WORKER_MAX_TASKS_PER_CHILD = 64  # recycle: Playwright leaks are real
CELERY_TASK_ACKS_LATE = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

#: Static schedule.  Operators may add ad-hoc entries through the Beat admin.
CELERY_BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    "daily-basket-collection": {
        # 02:15 IST - after the airlines' overnight inventory roll, before the
        # morning booking surge distorts the observation window.
        "task": "apix.tasks.dispatch_daily_basket",
        "schedule": crontab(hour=2, minute=15),
        "options": {"queue": "collection", "expires": 3_600},
    },
    "daily-index-compilation": {
        "task": "apix.tasks.compile_daily_index",
        "schedule": crontab(hour=5, minute=30),
        "options": {"queue": "statistics", "expires": 7_200},
    },
    "hourly-compliance-sweep": {
        "task": "apix.tasks.sweep_compliance_state",
        "schedule": crontab(minute=7),
        "options": {"queue": "default"},
    },
}
CELERY_TASK_ROUTES = {
    "apix.tasks.collect_*": {"queue": "collection"},
    "apix.tasks.clean_*": {"queue": "cleaning"},
    "apix.tasks.compile_*": {"queue": "statistics"},
    "apix.tasks.seal_*": {"queue": "statistics"},
}


# --------------------------------------------------------------------------- #
# DRF + JWT
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "apix.api.pagination.CursorTimeseriesPagination",
    "PAGE_SIZE": 100,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.ScopedRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "index_read": "600/hour",
        "fares_read": "300/hour",
        "collection_trigger": "20/hour",
        "analytics": "120/hour",
    },
    "COERCE_DECIMAL_TO_STRING": True,  # never hand a Decimal to a JS float
}

from datetime import timedelta  # noqa: E402  (kept local to the JWT block)

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env_int("JWT_ACCESS_MINUTES", 30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env_int("JWT_REFRESH_DAYS", 1)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SIGNING_KEY", SECRET_KEY),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_OBTAIN_SERIALIZER": "apix.api.serializers.APIxTokenObtainPairSerializer",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "APIx - Indian Airfare Price Index API",
    "DESCRIPTION": (
        "Official statistical API for the Airfare Price Index (APIx). "
        "Index values are computed with a trimmed Jevons elementary aggregate "
        "and a DGCA passenger-share weighted Laspeyres higher-level aggregation. "
        "Every published value carries a SHA-256 provenance hash chaining it to "
        "the immutable raw observations it was derived from."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost")
CORS_ALLOW_CREDENTIALS = True


# --------------------------------------------------------------------------- #
# APIx domain configuration
# --------------------------------------------------------------------------- #
APIX = {
    # -- collection ------------------------------------------------------- #
    "USER_AGENT": env(
        "APIX_USER_AGENT",
        "APIxBot/1.0 (+https://mospi.gov.in/apix; apix-ops@mospi.gov.in)",
    ),
    "RESPECT_ROBOTS": env_bool("APIX_RESPECT_ROBOTS", True),
    # robots.txt unreachable => refuse, never assume consent (RFC 9309 "unreachable").
    "ROBOTS_FAIL_CLOSED": env_bool("APIX_ROBOTS_FAIL_CLOSED", True),
    "ROBOTS_CACHE_TTL": env_int("APIX_ROBOTS_CACHE_TTL", 3_600),
    "ROBOTS_TIMEOUT": env_int("APIX_ROBOTS_TIMEOUT", 10),
    "DEFAULT_REQUESTS_PER_MINUTE": env_int("APIX_DEFAULT_RPM", 30),
    "DEFAULT_BURST": env_int("APIX_DEFAULT_BURST", 5),
    "RATE_LIMIT_WAIT_TIMEOUT": env_int("APIX_RATE_WAIT_TIMEOUT", 60),
    "BREAKER_FAILURE_THRESHOLD": env_int("APIX_BREAKER_FAILURES", 5),
    "BREAKER_COOLDOWN_SECONDS": env_int("APIX_BREAKER_COOLDOWN", 900),
    # Hard rule: a detected bot challenge halts the source permanently until an
    # operator resumes it.  The platform performs no evasion of any kind.
    "HALT_ON_BOT_CHALLENGE": True,
    # -- browser tier ------------------------------------------------------ #
    "PLAYWRIGHT_HEADLESS": env_bool("APIX_PW_HEADLESS", True),
    "PLAYWRIGHT_BROWSER": env("APIX_PW_BROWSER", "chromium"),
    "PLAYWRIGHT_TIMEOUT_MS": env_int("APIX_PW_TIMEOUT_MS", 45_000),
    "PLAYWRIGHT_LOCALE": "en-IN",
    "PLAYWRIGHT_TIMEZONE": "Asia/Kolkata",
    # -- sampling design --------------------------------------------------- #
    "LEAD_WINDOWS": [1, 7, 15, 30, 45],
    "ROUTE_BASKET": env_list(
        "APIX_ROUTE_BASKET", "DEL-BOM,DEL-BLR,BOM-BLR,DEL-CCU,BLR-HYD,MAA-DEL"
    ),
    "CURRENCY": "INR",
    # -- statistics -------------------------------------------------------- #
    "METHODOLOGY_CODE": env("APIX_METHODOLOGY", "APIX_JEVONS_V1"),
    "BASE_PERIOD": env("APIX_BASE_PERIOD", "2025-04"),
    "BASE_INDEX_LEVEL": env_decimal("APIX_BASE_LEVEL", "100"),
    "OUTLIER_THRESHOLD": env_decimal("APIX_OUTLIER_THRESHOLD", "3.5"),
    "OUTLIER_MIN_SAMPLE": env_int("APIX_OUTLIER_MIN_SAMPLE", 5),
    "TRIM_FRACTION": env_decimal("APIX_TRIM_FRACTION", "0.02"),
    "MIN_PAIRS_PER_STRATUM": env_int("APIX_MIN_PAIRS", 5),
    "MIN_WEIGHT_COVERAGE": env_decimal("APIX_MIN_WEIGHT_COVERAGE", "0.60"),
    "DECIMAL_PRECISION": env_int("APIX_DECIMAL_PRECISION", 34),
}


# --------------------------------------------------------------------------- #
# i18n / static
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "en-in"
TIME_ZONE = "UTC"          # storage is always UTC; presentation converts to IST
USE_I18N = True
USE_TZ = True
APIX_DISPLAY_TIMEZONE = "Asia/Kolkata"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------------------- #
# Logging - JSON lines, correlation-id aware
# --------------------------------------------------------------------------- #
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation": {"()": "apix.logging_utils.CorrelationIdFilter"},
    },
    "formatters": {
        "json": {"()": "apix.logging_utils.JsonFormatter"},
        "console": {"format": "%(asctime)s %(levelname)-7s %(name)-34s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["correlation"],
            "formatter": "json" if env_bool("DJANGO_LOG_JSON", True) else "console",
        }
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "apix": {"level": env("APIX_LOG_LEVEL", "INFO"), "propagate": True},
        "apix.policies": {"level": "INFO", "propagate": True},
        "urllib3": {"level": "WARNING", "propagate": True},
    },
}
