"""Developer / docker-compose settings."""

from __future__ import annotations

import os

# Allow synchronous Django ORM operations inside Playwright's asyncio event loop
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

from .base import *  # noqa: F403
from .base import APIX, REST_FRAMEWORK, env_bool

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Browsable API is genuinely useful while wiring the React client.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
}

# Local runs use the deterministic MockCollector unless explicitly told not to.
APIX = {**APIX, "PREFER_MOCK_SOURCES": env_bool("APIX_PREFER_MOCK", True)}

# Eager Celery for a single-process debugging session.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"