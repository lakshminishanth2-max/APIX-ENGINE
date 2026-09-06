"""Dynamic source registry and collector factory.

Collectors self-register at import time::

    @SourceRegistry.register("indigo_web", source_type=SourceType.AIRLINE_DIRECT)
    class IndiGoCollector(BaseCollector):
        ...

The scheduler never imports a concrete collector; it asks the registry for an
instance by ``source_id`` (or for every collector matching a tag / source type).
Adding a new source therefore means adding one module under
``app.collectors`` - no wiring change anywhere else.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Final, TypeVar

from app.collectors.base import BaseCollector, PolicyGuard, SourceType

__all__ = [
    "CollectorEntry",
    "DuplicateSourceError",
    "RegistryError",
    "SourceRegistry",
    "UnknownSourceError",
]

logger = logging.getLogger(__name__)

C = TypeVar("C", bound=BaseCollector)

_DEFAULT_PACKAGE: Final[str] = "app.collectors"
_UTC: Final = timezone.utc


class RegistryError(RuntimeError):
    """Base class for registry faults."""


class UnknownSourceError(RegistryError, KeyError):
    """Requested ``source_id`` has never been registered."""

    def __init__(self, source_id: str, known: Iterable[str]) -> None:
        self.source_id = source_id
        self.known = tuple(sorted(known))
        super().__init__(f"unknown source_id {source_id!r}; registered: {', '.join(self.known) or '<none>'}")

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return self.args[0]


class DuplicateSourceError(RegistryError):
    """A second collector tried to claim an already-registered ``source_id``."""


@dataclass(frozen=True, slots=True)
class CollectorEntry:
    """Immutable registry row describing one registered collector class."""

    source_id: str
    collector_cls: type[BaseCollector]
    source_type: SourceType
    tags: frozenset[str] = frozenset()
    enabled: bool = True
    defaults: Mapping[str, Any] = field(default_factory=dict)
    module: str = ""
    registered_at: datetime = field(default_factory=lambda: datetime.now(_UTC))

    def describe(self) -> dict[str, Any]:
        """JSON-safe summary used by the ``/admin/sources`` endpoint."""
        return {
            "source_id": self.source_id,
            "source_type": str(self.source_type),
            "class": f"{self.collector_cls.__module__}.{self.collector_cls.__qualname__}",
            "tags": sorted(self.tags),
            "enabled": self.enabled,
            "capabilities": self.collector_cls.capabilities.model_dump(mode="json"),
            "registered_at": self.registered_at.isoformat(),
        }


class SourceRegistry:
    """Process-wide, thread-safe registry of collector classes.

    All members are classmethods: the registry is a singleton by design because
    registration happens as a side effect of module import.
    """

    _entries: dict[str, CollectorEntry] = {}
    _lock: threading.RLock = threading.RLock()
    _discovered_packages: set[str] = set()

    # -- registration ------------------------------------------------------- #
    @classmethod
    def register(
        cls,
        source_id: str | None = None,
        *,
        source_type: SourceType | None = None,
        tags: Iterable[str] = (),
        enabled: bool = True,
        defaults: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> Callable[[type[C]], type[C]]:
        """Class decorator that binds a collector class to a ``source_id``.

        ``source_id`` / ``source_type`` default to the class attributes, so the
        decorator can be used bare (``@SourceRegistry.register()``).
        ``replace=True`` is intended for test doubles and hot-swap fixtures.
        """

        def decorator(collector_cls: type[C]) -> type[C]:
            if not (inspect.isclass(collector_cls) and issubclass(collector_cls, BaseCollector)):
                raise RegistryError(f"{collector_cls!r} is not a BaseCollector subclass")
            if inspect.isabstract(collector_cls):
                raise RegistryError(f"cannot register abstract collector {collector_cls.__name__}")

            resolved_id = (source_id or collector_cls.source_id or "").strip().lower()
            if not resolved_id:
                raise RegistryError(f"{collector_cls.__name__} has no source_id to register under")

            resolved_type = source_type or collector_cls.source_type
            # Keep the class attributes authoritative and in sync with the row.
            collector_cls.source_id = resolved_id
            collector_cls.source_type = resolved_type

            entry = CollectorEntry(
                source_id=resolved_id,
                collector_cls=collector_cls,
                source_type=resolved_type,
                tags=frozenset(t.strip().lower() for t in tags if t.strip()),
                enabled=enabled,
                defaults=dict(defaults or {}),
                module=collector_cls.__module__,
            )

            with cls._lock:
                existing = cls._entries.get(resolved_id)
                if existing is not None and not replace:
                    if existing.collector_cls is collector_cls:
                        return collector_cls  # idempotent re-import
                    raise DuplicateSourceError(
                        f"source_id {resolved_id!r} already registered by "
                        f"{existing.collector_cls.__module__}.{existing.collector_cls.__qualname__}"
                    )
                cls._entries[resolved_id] = entry

            logger.debug("registered collector source_id=%s class=%s", resolved_id, collector_cls.__qualname__)
            return collector_cls

        return decorator

    @classmethod
    def unregister(cls, source_id: str) -> bool:
        """Remove a registration.  Returns ``True`` if a row was dropped."""
        with cls._lock:
            return cls._entries.pop(source_id.strip().lower(), None) is not None

    @classmethod
    def clear(cls) -> None:
        """Drop every registration (test fixtures only)."""
        with cls._lock:
            cls._entries.clear()
            cls._discovered_packages.clear()

    # -- lookup ------------------------------------------------------------- #
    @classmethod
    def entry(cls, source_id: str) -> CollectorEntry:
        key = source_id.strip().lower()
        with cls._lock:
            try:
                return cls._entries[key]
            except KeyError:
                raise UnknownSourceError(key, cls._entries) from None

    @classmethod
    def get(cls, source_id: str) -> type[BaseCollector]:
        return cls.entry(source_id).collector_cls

    @classmethod
    def entries(
        cls,
        *,
        source_type: SourceType | None = None,
        tags: Iterable[str] | None = None,
        enabled_only: bool = True,
    ) -> tuple[CollectorEntry, ...]:
        wanted = frozenset(t.strip().lower() for t in tags) if tags is not None else None
        with cls._lock:
            rows = tuple(cls._entries.values())
        return tuple(
            sorted(
                (
                    row
                    for row in rows
                    if (not enabled_only or row.enabled)
                    and (source_type is None or row.source_type is source_type)
                    and (wanted is None or wanted <= row.tags)
                ),
                key=lambda row: row.source_id,
            )
        )

    @classmethod
    def source_ids(cls, *, enabled_only: bool = True) -> tuple[str, ...]:
        return tuple(row.source_id for row in cls.entries(enabled_only=enabled_only))

    @classmethod
    def describe_all(cls) -> list[dict[str, Any]]:
        return [row.describe() for row in cls.entries(enabled_only=False)]

    def __iter__(self) -> Iterator[CollectorEntry]:  # pragma: no cover - convenience
        return iter(type(self).entries(enabled_only=False))

    # -- factory ------------------------------------------------------------ #
    @classmethod
    def create(
        cls,
        source_id: str,
        *,
        policy: PolicyGuard | None = None,
        **overrides: Any,
    ) -> BaseCollector:
        """Instantiate one collector, merging registry defaults with overrides."""
        row = cls.entry(source_id)
        kwargs: dict[str, Any] = {**row.defaults, **overrides}
        if policy is not None:
            kwargs.setdefault("policy", policy)
        try:
            return row.collector_cls(**kwargs)
        except TypeError as exc:
            raise RegistryError(
                f"cannot construct {row.collector_cls.__qualname__} for source_id={row.source_id!r}: {exc}"
            ) from exc

    @classmethod
    def create_all(
        cls,
        *,
        source_type: SourceType | None = None,
        tags: Iterable[str] | None = None,
        policy: PolicyGuard | None = None,
        **overrides: Any,
    ) -> tuple[BaseCollector, ...]:
        """Instantiate every enabled collector matching the filter."""
        return tuple(
            cls.create(row.source_id, policy=policy, **overrides)
            for row in cls.entries(source_type=source_type, tags=tags)
        )

    # -- discovery ---------------------------------------------------------- #
    @classmethod
    def discover(cls, package: str = _DEFAULT_PACKAGE, *, force: bool = False) -> tuple[str, ...]:
        """Import every submodule of ``package`` so decorators run.

        Called once during FastAPI startup.  Import failures are logged and
        skipped so a single broken source cannot take the API down.
        """
        with cls._lock:
            already = package in cls._discovered_packages and not force
        if already:
            return cls.source_ids(enabled_only=False)

        try:
            pkg: ModuleType = importlib.import_module(package)
        except ImportError as exc:  # pragma: no cover - misconfiguration
            raise RegistryError(f"collector package {package!r} is not importable: {exc}") from exc

        for module_info in pkgutil.walk_packages(getattr(pkg, "__path__", []), prefix=f"{package}."):
            name = module_info.name
            if name.rsplit(".", 1)[-1].startswith("_"):
                continue
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001 - one bad source must not break startup
                logger.exception("failed to import collector module %s", name)

        with cls._lock:
            cls._discovered_packages.add(package)

        ids = cls.source_ids(enabled_only=False)
        logger.info("collector discovery complete package=%s sources=%s", package, list(ids))
        return ids
