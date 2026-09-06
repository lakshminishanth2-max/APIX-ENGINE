"""Dynamic source registry - plug-and-play collectors.

A new carrier is added by dropping one module into ``apix/collectors/`` and
decorating its class::

    @SourceRegistry.register("indigo_api", kind=SourceKind.DIRECT_API,
                             tags=("carrier", "lcc"))
    class IndiGoCollector(BaseCollector):
        ...

``AppConfig.ready()`` calls :meth:`SourceRegistry.autodiscover`, which imports
every module in the package so the decorators execute.  No core file changes,
no settings edits, no migration.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

from apix.collectors.base import BaseCollector, PolicyGate
from apix.enums import SourceKind

__all__ = ["CollectorEntry", "DuplicateSourceError", "SourceRegistry", "UnknownSourceError"]

logger = logging.getLogger(__name__)

C = TypeVar("C", bound=BaseCollector)
_PACKAGE = "apix.collectors"


class RegistryError(RuntimeError):
    """Base class for registry faults."""


class UnknownSourceError(RegistryError, KeyError):
    def __init__(self, source_code: str, known: Iterable[str]) -> None:
        self.source_code = source_code
        self.known = tuple(sorted(known))
        super().__init__(
            f"no collector registered for source_code {source_code!r}; "
            f"registered: {', '.join(self.known) or '<none>'}"
        )

    def __str__(self) -> str:
        return self.args[0]


class DuplicateSourceError(RegistryError):
    """Two collector classes claimed the same ``source_code``."""


@dataclass(frozen=True, slots=True)
class CollectorEntry:
    source_code: str
    collector_cls: type[BaseCollector]
    kind: str
    tags: frozenset[str] = frozenset()
    enabled: bool = True
    defaults: dict[str, Any] = field(default_factory=dict)
    module: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "source_code": self.source_code,
            "kind": self.kind,
            "class": f"{self.collector_cls.__module__}.{self.collector_cls.__qualname__}",
            "requires_browser": self.collector_cls.requires_browser,
            "cabins": sorted(self.collector_cls.supported_cabins),
            "tags": sorted(self.tags),
            "enabled": self.enabled,
        }


class SourceRegistry:
    """Process-wide, thread-safe catalogue of collector classes."""

    _entries: ClassVar[dict[str, CollectorEntry]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()
    _discovered: ClassVar[bool] = False

    # -- registration ------------------------------------------------------- #
    @classmethod
    def register(
        cls,
        source_code: str | None = None,
        *,
        kind: str | None = None,
        tags: Iterable[str] = (),
        enabled: bool = True,
        defaults: dict[str, Any] | None = None,
        replace: bool = False,
    ) -> Callable[[type[C]], type[C]]:
        """Class decorator binding a collector to a ``Source.code``."""

        def decorator(collector_cls: type[C]) -> type[C]:
            if not (inspect.isclass(collector_cls) and issubclass(collector_cls, BaseCollector)):
                raise RegistryError(f"{collector_cls!r} is not a BaseCollector subclass")
            if inspect.isabstract(collector_cls):
                raise RegistryError(f"cannot register abstract collector {collector_cls.__name__}")

            code = (source_code or collector_cls.source_code or "").strip().lower()
            if not code:
                raise RegistryError(f"{collector_cls.__name__} has no source_code to register under")

            resolved_kind = kind or collector_cls.kind or SourceKind.PERMITTED_WEB
            collector_cls.source_code = code
            collector_cls.kind = resolved_kind

            entry = CollectorEntry(
                source_code=code,
                collector_cls=collector_cls,
                kind=resolved_kind,
                tags=frozenset(tag.strip().lower() for tag in tags if tag.strip()),
                enabled=enabled,
                defaults=dict(defaults or {}),
                module=collector_cls.__module__,
            )

            with cls._lock:
                existing = cls._entries.get(code)
                if existing is not None and not replace:
                    if existing.collector_cls is collector_cls:
                        return collector_cls  # idempotent re-import
                    raise DuplicateSourceError(
                        f"source_code {code!r} already registered by "
                        f"{existing.collector_cls.__module__}.{existing.collector_cls.__qualname__}"
                    )
                cls._entries[code] = entry

            logger.debug("registered collector", extra={"source": code, "cls": collector_cls.__name__})
            return collector_cls

        return decorator

    @classmethod
    def unregister(cls, source_code: str) -> bool:
        with cls._lock:
            return cls._entries.pop(source_code.strip().lower(), None) is not None

    @classmethod
    def clear(cls) -> None:
        """Test-fixture helper."""
        with cls._lock:
            cls._entries.clear()
            cls._discovered = False

    # -- lookup ------------------------------------------------------------- #
    @classmethod
    def entry(cls, source_code: str) -> CollectorEntry:
        key = source_code.strip().lower()
        with cls._lock:
            try:
                return cls._entries[key]
            except KeyError:
                raise UnknownSourceError(key, cls._entries) from None

    @classmethod
    def get(cls, source_code: str) -> type[BaseCollector]:
        return cls.entry(source_code).collector_cls

    @classmethod
    def has(cls, source_code: str) -> bool:
        return source_code.strip().lower() in cls._entries

    @classmethod
    def entries(
        cls, *, kind: str | None = None, tags: Iterable[str] | None = None, enabled_only: bool = True
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
                    and (kind is None or row.kind == kind)
                    and (wanted is None or wanted <= row.tags)
                ),
                key=lambda row: row.source_code,
            )
        )

    @classmethod
    def source_codes(cls) -> tuple[str, ...]:
        return tuple(entry.source_code for entry in cls.entries(enabled_only=False))

    @classmethod
    def describe_all(cls) -> list[dict[str, Any]]:
        return [entry.describe() for entry in cls.entries(enabled_only=False)]

    def __iter__(self) -> Iterator[CollectorEntry]:  # pragma: no cover
        return iter(type(self).entries(enabled_only=False))

    # -- factory ------------------------------------------------------------ #
    @classmethod
    def create(
        cls, source_code: str, *, policy: PolicyGate | None = None, **overrides: Any
    ) -> BaseCollector:
        """Instantiate a collector, merging registry defaults with overrides."""
        entry = cls.entry(source_code)
        kwargs: dict[str, Any] = {**entry.defaults, **overrides}
        if policy is not None:
            kwargs.setdefault("policy", policy)
        try:
            return entry.collector_cls(**kwargs)
        except TypeError as exc:
            raise RegistryError(
                f"cannot construct {entry.collector_cls.__qualname__} for {source_code!r}: {exc}"
            ) from exc

    @classmethod
    def create_for_source(cls, source: Any, *, policy: PolicyGate | None = None, **overrides: Any) -> BaseCollector:
        """Build the collector for a :class:`apix.models.Source` row."""
        return cls.create(
            source.code,
            policy=policy,
            base_url=overrides.pop("base_url", None) or getattr(source, "base_url", ""),
            **overrides,
        )

    # -- discovery ---------------------------------------------------------- #
    @classmethod
    def autodiscover(cls, package: str = _PACKAGE, *, force: bool = False) -> tuple[str, ...]:
        """Import every collector module so its decorator runs.

        A single broken collector must never prevent Django from starting, so
        import errors are logged and skipped rather than raised.
        """
        with cls._lock:
            if cls._discovered and not force:
                return cls.source_codes()

        pkg = importlib.import_module(package)
        for module_info in pkgutil.iter_modules(getattr(pkg, "__path__", [])):
            name = module_info.name
            if name.startswith("_") or name in {"base", "registry"}:
                continue
            try:
                importlib.import_module(f"{package}.{name}")
            except Exception:  # noqa: BLE001
                logger.exception("failed to import collector module", extra={"collector_module": name})

        with cls._lock:
            cls._discovered = True
        codes = cls.source_codes()
        logger.info("collector autodiscovery complete", extra={"count": len(codes)})
        return codes
