"""JSON logging with a correlation id that survives the request -> task hop."""

from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any, Final

__all__ = ["CorrelationIdFilter", "JsonFormatter", "bind_correlation_id", "get_correlation_id"]

_UTC: Final = timezone.utc

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apix_correlation_id", default=None
)

_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName", "correlation_id",
    }
)


def bind_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


class CorrelationIdFilter(logging.Filter):
    """Attaches the ambient correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line for the cluster log shipper."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=_UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        return json.dumps(payload, ensure_ascii=False, default=str)
