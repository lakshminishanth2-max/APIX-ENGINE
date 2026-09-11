from __future__ import annotations
import os
"""Hardened settings for the NSO/MoSPI deployment."""


from .base import *  # noqa: F403
from .base import APIX, REST_FRAMEWORK, env, env_bool, env_int

DEBUG = False

# Secrets must be injected; no fallbacks in production.
SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
JWT_SIGNING_KEY = env("JWT_SIGNING_KEY", required=True)
SIMPLE_JWT = {**globals()["SIMPLE_JWT"], "SIGNING_KEY": JWT_SIGNING_KEY}

# -- transport security ----------------------------------------------------- #
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 31_536_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

# -- API surface ------------------------------------------------------------ #
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
}

# -- statistical guardrails ------------------------------------------------- #
# Production never silently substitutes synthetic data for a live source.
APIX = {**APIX, "PREFER_MOCK_SOURCES": os.getenv("APIX_PREFER_MOCK_SOURCES", "False").lower() in ("true", "1"), "ALLOW_MOCK_IN_INDEX": os.getenv("APIX_ALLOW_MOCK_IN_INDEX", "False").lower() in ("true", "1")}

DATABASES = {  # noqa: F405
    **globals()["DATABASES"],
}
DATABASES["default"]["OPTIONS"] = {
    **DATABASES["default"].get("OPTIONS", {}),
    "sslmode": env("POSTGRES_SSLMODE", "prefer"),
}
