"""Collector contracts.

A collector knows two things and nothing else:

* how to *fetch* one search - :meth:`BaseCollector.fetch` - returning the
  verbatim bytes the source served, wrapped in :class:`RawCollectorResponse`;
* how to *extract* fare rows out of that payload -
  :meth:`BaseCollector.extract` - returning loosely-typed dictionaries whose
  values may still be raw strings such as ``"₹ 4,521.00"``.

Everything else is the platform's job.  :meth:`BaseCollector.run` is a final
template method that gates the fetch through the :class:`PolicyEngine`, times
it, classifies failures, and feeds the response back into the compliance layer
so a bot challenge can halt the source.  Collectors never parse currency, never
decide what a missing value means, and never touch the database - that keeps
adding a new carrier to a single, reviewable file.
"""

from __future__ import annotations

import abc
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, ClassVar, Protocol, TypedDict, runtime_checkable
from uuid import UUID

from apix.enums import CabinClass, PayloadKind, SourceKind

__all__ = [
    "BaseCollector",
    "CollectionTask",
    "CollectorError",
    "CollectorOutcome",
    "ExtractedFare",
    "ParseError",
    "PolicyGate",
    "RawCollectorResponse",
    "TransportError",
    "sha256_bytes",
]

logger = logging.getLogger(__name__)
UTC = timezone.utc


def sha256_bytes(data: bytes | str) -> str:
    """Ingress hash: the anchor of every provenance chain in the platform."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CollectorError(RuntimeError):
    """Base class for collector failures."""

    retryable: bool = True

    def __init__(self, message: str, *, source_code: str = "", retryable: bool | None = None) -> None:
        super().__init__(message)
        self.source_code = source_code
        if retryable is not None:
            self.retryable = retryable


class TransportError(CollectorError):
    """Network, TLS, timeout or browser-navigation failure."""


class ParseError(CollectorError):
    """The source answered but the payload could not be interpreted."""

    retryable = False


# --------------------------------------------------------------------------- #
# Policy seam
# --------------------------------------------------------------------------- #
@runtime_checkable
class PolicyGate(Protocol):
    """Structural contract satisfied by ``apix.policies.engine.PolicyEngine``.

    Refusals are raised as exceptions carrying ``policy_blocked = True``, so the
    collector package never imports the policy package and the two can be
    tested in isolation.
    """

    def authorize(self, *, source_code: str, url: str, tokens: int = 1) -> Any: ...

    def observe_response(
        self,
        *,
        source_code: str,
        url: str,
        status_code: int,
        headers: dict[str, str] | None = None,
        body_snippet: str | None = None,
    ) -> None: ...

    def observe_exception(self, *, source_code: str, url: str, error: BaseException) -> None: ...


# --------------------------------------------------------------------------- #
# Work unit and results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CollectionTask:
    """One (route, departure date, lead window) search to execute."""

    route_code: str
    origin: str
    destination: str
    departure_date: date
    lead_window_days: int
    observation_date: date
    cabin: str = CabinClass.ECONOMY
    currency: str = "INR"
    max_results: int = 200
    run_id: UUID | None = None
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if self.origin == self.destination:
            raise ValueError(f"circular route: {self.origin}-{self.destination}")
        if self.lead_window_days < 0:
            raise ValueError("lead_window_days must be non-negative")

    @property
    def search_key(self) -> str:
        return (
            f"{self.route_code}|{self.departure_date.isoformat()}"
            f"|T+{self.lead_window_days}|{self.cabin}|{self.currency}"
        )


class ExtractedFare(TypedDict, total=False):
    """A fare row as the source describes it - values may still be strings.

    Absent keys are meaningful: a key that is *not present* is unknown, which
    the cleaning pipeline records as NULL.  A key present with the value ``0``
    is a genuine zero.  Collectors must never fill a gap with ``0``.
    """

    carrier_iata: str
    flight_number: str
    cabin: str
    departure_local: str
    arrival_local: str
    currency: str
    base_fare: Any
    udf: Any
    psf: Any
    asf: Any
    gst: Any
    other_charges: Any
    taxes_fees: Any
    total_fare: Any
    seats_remaining: Any
    inventory_status: str
    fare_basis: str
    booking_class: str
    is_refundable: Any
    raw_row_hash: str


@dataclass(frozen=True, slots=True)
class RawCollectorResponse:
    """Verbatim capture destined for the immutable ``RawObservation`` table."""

    payload: dict[str, Any] | list[Any]
    request_url: str
    payload_kind: str = PayloadKind.JSON
    request_method: str = "GET"
    http_status: int | None = 200
    response_headers: dict[str, str] = field(default_factory=dict)
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    collector: str = ""
    content_bytes: int = 0

    @property
    def canonical_bytes(self) -> bytes:
        """Stable serialisation used for the ingress hash."""
        return json.dumps(
            self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode("utf-8")

    @property
    def ingress_hash(self) -> str:
        return sha256_bytes(self.canonical_bytes)

    def sized(self) -> RawCollectorResponse:
        """Return a copy with ``content_bytes`` filled in."""
        if self.content_bytes:
            return self
        return RawCollectorResponse(
            payload=self.payload,
            request_url=self.request_url,
            payload_kind=self.payload_kind,
            request_method=self.request_method,
            http_status=self.http_status,
            response_headers=self.response_headers,
            captured_at=self.captured_at,
            collector=self.collector,
            content_bytes=len(self.canonical_bytes),
        )


@dataclass(frozen=True, slots=True)
class CollectorOutcome:
    """What one :meth:`BaseCollector.run` produced, success or failure."""

    task: CollectionTask
    source_code: str
    ok: bool
    response: RawCollectorResponse | None = None
    fares: tuple[ExtractedFare, ...] = ()
    error: str = ""
    error_kind: str = ""
    blocked_by_policy: bool = False
    halted: bool = False
    duration_ms: int = 0

    @property
    def fare_count(self) -> int:
        return len(self.fares)


# --------------------------------------------------------------------------- #
# Base collector
# --------------------------------------------------------------------------- #
class BaseCollector(abc.ABC):
    """Template-method base for every source integration."""

    #: Must equal the ``Source.code`` row in the registry table.
    source_code: ClassVar[str] = ""
    kind: ClassVar[str] = SourceKind.PERMITTED_WEB
    #: Playwright-backed collectors set this so the worker pool can size itself.
    requires_browser: ClassVar[bool] = False
    supported_cabins: ClassVar[frozenset[str]] = frozenset({CabinClass.ECONOMY})
    max_lead_window_days: ClassVar[int] = 365

    def __init__(self, *, policy: PolicyGate | None = None, base_url: str = "", **options: Any) -> None:
        self.policy = policy
        self.base_url = base_url.rstrip("/")
        self.options = options
        self.log = logger.getChild(self.source_code or type(self).__name__)

    #: Methods a concrete collector must implement before it can be registered.
    _REQUIRED_IMPLEMENTATIONS: ClassVar[tuple[str, ...]] = ("fetch", "extract")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # ``__abstractmethods__`` is not populated until ABCMeta finishes, and it
        # is a metaclass descriptor rather than an inherited attribute, so the
        # abstractness of each required method is checked directly.  Intermediate
        # base classes (e.g. PlaywrightCollector) legitimately have no source_code.
        still_abstract = any(
            getattr(getattr(cls, name, None), "__isabstractmethod__", False)
            for name in cls._REQUIRED_IMPLEMENTATIONS
        )
        if not still_abstract and not cls.source_code:
            raise TypeError(f"{cls.__name__} must define a non-empty 'source_code'")

    # -- lifecycle ---------------------------------------------------------- #
    def open(self) -> None:  # noqa: B027 - optional hook, deliberately not abstract
        """Acquire expensive resources (browser, session).  Optional."""

    def close(self) -> None:  # noqa: B027 - optional hook, deliberately not abstract
        """Release resources.  Must be idempotent."""

    def __enter__(self) -> BaseCollector:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public entry point ------------------------------------------------- #
    def run(self, task: CollectionTask) -> CollectorOutcome:
        """Execute one task.  Never raises: failures become a typed outcome."""
        started = time.perf_counter()
        url = self.search_url(task)

        try:
            self.validate_task(task)
            if self.policy is not None:
                self.policy.authorize(source_code=self.source_code, url=url)

            response = self.fetch(task).sized()

            if self.policy is not None and response.http_status is not None:
                self.policy.observe_response(
                    source_code=self.source_code,
                    url=url,
                    status_code=response.http_status,
                    headers=response.response_headers,
                    body_snippet=self._body_snippet(response),
                )

            fares = tuple(self.extract(response.payload, task))
            return CollectorOutcome(
                task=task,
                source_code=self.source_code,
                ok=True,
                response=response,
                fares=fares[: task.max_results],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        except Exception as exc:  # noqa: BLE001 - deliberate failure boundary
            blocked = bool(getattr(exc, "policy_blocked", False))
            halted = bool(getattr(exc, "halted", False))
            if self.policy is not None and not blocked:
                try:
                    self.policy.observe_exception(source_code=self.source_code, url=url, error=exc)
                except Exception:  # noqa: BLE001 - never mask the original error
                    self.log.exception("policy engine failed while recording an exception")

            self.log.warning(
                "collection failed",
                extra={
                    "source": self.source_code,
                    "route": task.route_code,
                    "lead_window": task.lead_window_days,
                    "error_kind": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return CollectorOutcome(
                task=task,
                source_code=self.source_code,
                ok=False,
                error=str(exc),
                error_kind=type(exc).__name__,
                blocked_by_policy=blocked,
                halted=halted,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    # -- subclass seam ------------------------------------------------------ #
    @abc.abstractmethod
    def fetch(self, task: CollectionTask) -> RawCollectorResponse:
        """Perform the search and return the verbatim payload."""

    @abc.abstractmethod
    def extract(
        self, payload: dict[str, Any] | list[Any], task: CollectionTask
    ) -> list[ExtractedFare]:
        """Pull fare rows out of a payload.  Pure - no I/O, no DB."""

    def search_url(self, task: CollectionTask) -> str:
        """URL presented to the policy engine for robots and rate decisions."""
        return self.base_url or f"https://{self.source_code}.invalid/"

    # -- helpers ------------------------------------------------------------ #
    def validate_task(self, task: CollectionTask) -> None:
        if task.cabin not in self.supported_cabins:
            raise ParseError(
                f"{self.source_code} does not serve cabin {task.cabin}", source_code=self.source_code
            )
        if task.lead_window_days > self.max_lead_window_days:
            raise ParseError(
                f"{self.source_code} does not serve T+{task.lead_window_days}",
                source_code=self.source_code,
            )

    @staticmethod
    def _body_snippet(response: RawCollectorResponse, limit: int = 4_096) -> str:
        """Text handed to the bot-challenge detector."""
        if isinstance(response.payload, dict) and "body" in response.payload:
            return str(response.payload["body"])[:limit]
        return response.canonical_bytes[:limit].decode("utf-8", errors="replace")

    def health_check(self) -> dict[str, Any]:
        return {"source_code": self.source_code, "kind": self.kind, "healthy": True}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} source_code={self.source_code!r}>"
