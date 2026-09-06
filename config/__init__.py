"""Django project package.

Importing the Celery app here guarantees that ``@shared_task`` decorators bind
to the configured application whenever Django starts, regardless of whether the
process is a web worker, a Celery worker or a management command.
"""

from __future__ import annotations

from config.celery import app as celery_app

__all__ = ["celery_app"]
