"""Application configuration and startup wiring."""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_EVEN, getcontext
from typing import Any

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class ApixConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apix"
    verbose_name = "Airfare Price Index"

    def ready(self) -> None:
        """Run once per process, in web workers and Celery workers alike."""
        # 1. Decimal context.  Every worker must agree on precision and rounding
        #    or two nodes could compute different index levels from identical
        #    inputs.  Banker's rounding is the statistical convention.
        context = getcontext()
        context.prec = int(settings.APIX.get("DECIMAL_PRECISION", 34))
        context.rounding = ROUND_HALF_EVEN

        # 2. Import collector modules so their @register decorators execute and
        #    the SourceRegistry is populated before any task runs.
        from apix.collectors.registry import SourceRegistry

        discovered = SourceRegistry.autodiscover()
        logger.info("collector registry ready", extra={"sources": list(discovered)})

        # 3. Connect signal receivers (audit fan-out).
        from apix import signals  # noqa: F401

    def _noop(self, *_: Any) -> None:  # pragma: no cover - placeholder hook
        return None
