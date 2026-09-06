"""Structured JSON logging with request/job correlation.

Every log line carries ``correlation_id`` when one is in scope, so a single
collection job can be traced from the gateway call, through the Celery worker,
into the compliance decisions and out to the sealed provenance record.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Final

__all__ = ["configure_logging", "correlation_id", "bind_correlation_id", "get_logger"]

_UTC: Final = timezone.utc

#: Propagates across ``await`` boundaries and into Celery task bodies.
correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apix_correlation_id", default=None
)

_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)


def bind_correlation_id(value: str | None) -> contextvars.Token[str | None]:
    """Bind a correlation id for the current context; reset with the token."""
    return correlation_id.set(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line - what the cluster's log shipper expects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=_UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["correlation_id"] = cid
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Readable single-line format for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-38s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        cid = correlation_id.get()
        base = super().format(record)
        return f"{base}  [cid={cid[:8]}]" if cid else base


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Install the root handler.  Idempotent - safe to call per worker fork."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party noise that would otherwise drown the compliance decisions.
    for noisy in ("httpx", "httpcore", "asyncio", "aiosqlite", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
