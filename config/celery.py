"""Celery application for the APIx distributed collection and statistics tiers.

Queue topology
--------------
``collection``  I/O- and browser-bound.  Low concurrency, long time limits, and
                ``worker_max_tasks_per_child`` recycling because Playwright
                browser processes accumulate memory.
``cleaning``    CPU-light, DB-heavy.  Higher concurrency.
``statistics``  Serialised by design: index compilation and provenance sealing
                append to a hash chain, so two concurrent compilations for the
                same series would race on the chain head.  Run this queue with
                ``--concurrency=1``.
``default``     Housekeeping, compliance sweeps, audit fan-out.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

# Ensure Windows asyncio supports subprocess creation for Playwright
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from celery import Celery, Task
from celery.signals import setup_logging, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("apix")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

logger = logging.getLogger(__name__)


@setup_logging.connect
def _configure_logging(**_: Any) -> None:
    """Let Django's LOGGING dict own formatting - Celery must not override it."""
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)


@task_prerun.connect
def _bind_correlation(task_id: str | None = None, task: Task | None = None, **kwargs: Any) -> None:
    """Propagate the correlation id from the request that queued the task."""
    from apix.logging_utils import bind_correlation_id

    headers = getattr(getattr(task, "request", None), "correlation_id", None)
    bind_correlation_id(headers or task_id)


@task_postrun.connect
def _unbind_correlation(**_: Any) -> None:
    from apix.logging_utils import bind_correlation_id

    bind_correlation_id(None)


@app.task(bind=True, name="apix.debug.ping")
def ping(self: Task) -> dict[str, str]:
    """Liveness probe used by the container healthcheck."""
    return {"status": "ok", "task_id": self.request.id or "eager"}