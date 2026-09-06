"""Source collectors.

Modules in this package are imported automatically at application start by
:meth:`apix.collectors.registry.SourceRegistry.autodiscover`, which is what
makes a new carrier a single-file change.
"""

from __future__ import annotations

from apix.collectors.base import (
    BaseCollector,
    CollectionTask,
    CollectorOutcome,
    ExtractedFare,
    RawCollectorResponse,
)
from apix.collectors.registry import SourceRegistry

__all__ = [
    "BaseCollector",
    "CollectionTask",
    "CollectorOutcome",
    "ExtractedFare",
    "RawCollectorResponse",
    "SourceRegistry",
]
