"""Collector contracts for the APIx ingestion tier.

Every source integration - airline direct, OTA, GDS feed or synthetic fixture -
implements :class:`BaseCollector`.  The base class owns the invariant parts of a
collection run (policy authorisation, timing, error classification, payload
digesting, capability validation) so concrete collectors only have to implement
transport and parsing in :meth:`BaseCollector._collect`.

The module deliberately has **no** dependency on the policy, cleaning or index
layers: the policy engine is injected through the structural
:class:`PolicyGuard` protocol, which keeps the ingestion tier testable in
isolation and prevents an import cycle with ``app.policies.compliance``.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from types import TracebackType
from typing import Any, ClassVar, Final, Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BaseCollector",
    "CabinClass",
    "CollectionRequest",
    "CollectorCapabilities",
    "CollectorError",
    "CollectorResult",
    "CollectorStatus",
    "FareQuote",
    "HealthReport",
    "ParseError",
    "PolicyGuard",
    "RunMetrics",
    "SourceType",
    "TransportError",
    "UnsupportedRequestError",
    "payload_digest",
]

logger = logging.getLogger(__name__)

_IATA_CODE_LEN: Final[int] = 3
_MAX_HORIZON_DAYS: Final[int] = 365
_UTC: Final = timezone.utc


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
class SourceType(StrEnum):
    """Provenance class of a source; drives trust ranking during dedup."""

    AIRLINE_DIRECT = "airline_direct"
    GDS = "gds"
    OTA = "ota"
    METASEARCH = "metasearch"
    REGULATORY = "regulatory"
    MOCK = "mock"


class CabinClass(StrEnum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class CollectorStatus(StrEnum):
    """Terminal state of a single collection run."""

    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    POLICY_BLOCKED = "policy_blocked"
    CIRCUIT_OPEN = "circuit_open"
    TRANSPORT_ERROR = "transport_error"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED = "unsupported"
    ERROR = "error"

    @property
    def is_success(self) -> bool:
        return self in (CollectorStatus.OK, CollectorStatus.PARTIAL, CollectorStatus.EMPTY)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CollectorError(RuntimeError):
    """Base class for every error raised inside a collector implementation."""

    status: ClassVar[CollectorStatus] = CollectorStatus.ERROR

    def __init__(self, message: str, *, source_id: str | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.source_id = source_id
        self.retryable = retryable


class TransportError(CollectorError):
    """Network / HTTP / protocol failure."""

    status: ClassVar[CollectorStatus] = CollectorStatus.TRANSPORT_ERROR


class ParseError(CollectorError):
    """Upstream responded but the payload could not be interpreted."""

    status: ClassVar[CollectorStatus] = CollectorStatus.PARSE_ERROR

    def __init__(self, message: str, *, source_id: str | None = None) -> None:
        super().__init__(message, source_id=source_id, retryable=False)


class UnsupportedRequestError(CollectorError):
    """The request falls outside the collector's declared capabilities."""

    status: ClassVar[CollectorStatus] = CollectorStatus.UNSUPPORTED

    def __init__(self, message: str, *, source_id: str | None = None) -> None:
        super().__init__(message, source_id=source_id, retryable=False)


# --------------------------------------------------------------------------- #
# Policy seam
# --------------------------------------------------------------------------- #
@runtime_checkable
class PolicyGuard(Protocol):
    """Structural contract satisfied by ``app.policies.compliance.PolicyEngine``.

    Implementations signal refusal by raising an exception carrying the
    ``policy_blocked`` attribute (and optionally ``circuit_open``); the base
    collector maps those onto :class:`CollectorStatus` without importing the
    policy package.
    """

    async def authorize(self, *, source_id: str, url: str, tokens: int = 1) -> Any: ...

    async def observe_response(
        self,
        *,
        source_id: str,
        url: str,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        body_snippet: str | None = None,
    ) -> None: ...

    async def observe_exception(self, *, source_id: str, url: str, error: BaseException) -> None: ...


# --------------------------------------------------------------------------- #
# Wire models
# --------------------------------------------------------------------------- #
_FROZEN = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


def payload_digest(payload: bytes | str) -> str:
    """SHA-256 of a raw upstream payload; the first link of the lineage chain."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(raw).hexdigest()


class CollectorCapabilities(BaseModel):
    """Declarative description of what a collector can actually serve."""

    model_config = _FROZEN

    supports_cabins: frozenset[CabinClass] = Field(default=frozenset({CabinClass.ECONOMY}))
    max_horizon_days: int = Field(default=45, ge=1, le=_MAX_HORIZON_DAYS)
    supports_one_way: bool = True
    supports_round_trip: bool = False
    supports_seat_availability: bool = False
    supports_fare_breakdown: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=64)
    requires_robots_check: bool = True
    currencies: frozenset[str] = Field(default=frozenset({"INR"}))


class CollectionRequest(BaseModel):
    """A unit of work handed to a collector by the scheduler."""

    model_config = _FROZEN

    origin: str
    destination: str
    departure_dates: tuple[date, ...] = Field(min_length=1)
    cabin: CabinClass = CabinClass.ECONOMY
    currency: str = "INR"
    observation_date: date = Field(default_factory=lambda: datetime.now(_UTC).date())
    max_results: int = Field(default=250, ge=1, le=5_000)
    carriers: tuple[str, ...] = ()
    correlation_id: UUID = Field(default_factory=uuid4)

    @field_validator("origin", "destination")
    @classmethod
    def _validate_iata(cls, value: str) -> str:
        code = value.upper()
        if len(code) != _IATA_CODE_LEN or not code.isalpha():
            raise ValueError(f"invalid IATA station code: {value!r}")
        return code

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        code = value.upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"invalid ISO-4217 currency: {value!r}")
        return code

    @field_validator("carriers")
    @classmethod
    def _validate_carriers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({c.upper() for c in value}))

    @model_validator(mode="after")
    def _validate_route(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        return self

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    def horizon_days(self) -> tuple[int, ...]:
        return tuple((d - self.observation_date).days for d in self.departure_dates)


class FareQuote(BaseModel):
    """One observed price point, pre-cleaning.

    ``None`` on a monetary component means *not published by the source* and is
    never interchangeable with ``Decimal("0")`` - the distinction is preserved
    all the way into ``app.cleaning.normalizer``.
    """

    model_config = _FROZEN

    quote_id: UUID = Field(default_factory=uuid4)
    source_id: str
    source_type: SourceType
    carrier_code: str
    flight_number: str | None = None
    origin: str
    destination: str
    departure_date: date
    departure_time: str | None = None
    observation_ts: datetime
    days_to_departure: int = Field(ge=0, le=_MAX_HORIZON_DAYS)
    cabin: CabinClass = CabinClass.ECONOMY
    currency: str = "INR"

    base_fare: Decimal | None = None
    taxes: Decimal | None = None
    fees: Decimal | None = None
    surcharges: Decimal | None = None
    total_fare: Decimal = Field(gt=Decimal("0"))

    seats_remaining: int | None = Field(default=None, ge=0)
    fare_basis: str | None = None
    booking_class: str | None = None
    is_refundable: bool | None = None

    raw_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attributes: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("carrier_code")
    @classmethod
    def _upper_carrier(cls, value: str) -> str:
        return value.upper()

    @field_validator("origin", "destination")
    @classmethod
    def _upper_station(cls, value: str) -> str:
        return value.upper()

    @field_validator("observation_ts")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observation_ts must be timezone-aware")
        return value.astimezone(_UTC)

    @field_validator("base_fare", "taxes", "fees", "surcharges")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("monetary components must be non-negative")
        return value

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"


class RunMetrics(BaseModel):
    model_config = _FROZEN

    latency_ms: int = Field(ge=0)
    requests_issued: int = Field(default=0, ge=0)
    bytes_received: int = Field(default=0, ge=0)
    quotes_emitted: int = Field(default=0, ge=0)
    policy_wait_ms: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)


class CollectorResult(BaseModel):
    """Everything a run produced, including the reason it produced nothing."""

    model_config = _FROZEN

    run_id: UUID
    source_id: str
    source_type: SourceType
    status: CollectorStatus
    request: CollectionRequest
    quotes: tuple[FareQuote, ...] = ()
    metrics: RunMetrics
    warnings: tuple[str, ...] = ()
    error: str | None = None
    started_at: datetime
    finished_at: datetime

    @property
    def ok(self) -> bool:
        return self.status.is_success


class HealthReport(BaseModel):
    model_config = _FROZEN

    source_id: str
    healthy: bool
    checked_at: datetime
    latency_ms: int | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Base collector
# --------------------------------------------------------------------------- #
class BaseCollector(abc.ABC):
    """Template-method base class for all APIx source collectors.

    Subclasses declare ``source_id`` / ``source_type`` and implement
    :meth:`_collect`.  They must not override :meth:`collect`, which provides
    the cross-cutting guarantees the platform depends on.
    """

    source_id: ClassVar[str] = ""
    source_type: ClassVar[SourceType] = SourceType.MOCK
    capabilities: ClassVar[CollectorCapabilities] = CollectorCapabilities()

    def __init__(
        self,
        *,
        policy: PolicyGuard | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._policy = policy
        self._base_url = (base_url or "").rstrip("/")
        self._timeout_s = timeout_s
        self._closed = False
        self._log = logger.getChild(self.source_id or type(self).__name__)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "__abstractmethods__", None) and not cls.source_id:
            raise TypeError(f"{cls.__name__} must define a non-empty class attribute 'source_id'")

    # -- lifecycle ---------------------------------------------------------- #
    async def __aenter__(self) -> Self:
        await self.startup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def startup(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Open transports / warm caches.  Override as needed."""

    async def aclose(self) -> None:
        """Release transports.  Override as needed; must stay idempotent."""
        self._closed = True

    # -- public API --------------------------------------------------------- #
    async def collect(self, request: CollectionRequest) -> CollectorResult:
        """Run one collection.  Never raises: failures become a typed result."""
        run_id = uuid4()
        started_at = datetime.now(_UTC)
        t0 = time.perf_counter()
        warnings: list[str] = []
        quotes: tuple[FareQuote, ...] = ()
        status = CollectorStatus.OK
        error: str | None = None
        policy_wait_ms = 0

        try:
            self.validate_request(request)
            if self._policy is not None:
                p0 = time.perf_counter()
                await self._policy.authorize(source_id=self.source_id, url=self.policy_url(request))
                policy_wait_ms = int((time.perf_counter() - p0) * 1000)

            produced = tuple(await self._collect(request))
            quotes = produced[: request.max_results]
            if len(produced) > len(quotes):
                warnings.append(f"truncated {len(produced) - len(quotes)} quotes at max_results")
            if not quotes:
                status = CollectorStatus.EMPTY
            elif warnings:
                status = CollectorStatus.PARTIAL
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - deliberate failure boundary
            status, error = self._classify(exc)
            self._log.warning(
                "collection failed source=%s route=%s status=%s error=%s",
                self.source_id,
                request.route,
                status,
                error,
                exc_info=status is CollectorStatus.ERROR,
            )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return CollectorResult(
            run_id=run_id,
            source_id=self.source_id,
            source_type=self.source_type,
            status=status,
            request=request,
            quotes=quotes,
            metrics=RunMetrics(
                latency_ms=elapsed_ms,
                quotes_emitted=len(quotes),
                policy_wait_ms=policy_wait_ms,
            ),
            warnings=tuple(warnings),
            error=error,
            started_at=started_at,
            finished_at=datetime.now(_UTC),
        )

    async def health_check(self) -> HealthReport:
        """Cheap liveness probe; overridden by network-backed collectors."""
        return HealthReport(
            source_id=self.source_id,
            healthy=not self._closed,
            checked_at=datetime.now(_UTC),
            detail="closed" if self._closed else "ready",
        )

    def validate_request(self, request: CollectionRequest) -> None:
        """Reject work the collector has not declared support for."""
        caps = self.capabilities
        if request.cabin not in caps.supports_cabins:
            raise UnsupportedRequestError(
                f"cabin {request.cabin} not supported by {self.source_id}", source_id=self.source_id
            )
        if request.currency not in caps.currencies:
            raise UnsupportedRequestError(
                f"currency {request.currency} not supported by {self.source_id}", source_id=self.source_id
            )
        overshoot = [d for d in request.horizon_days() if d > caps.max_horizon_days or d < 0]
        if overshoot:
            raise UnsupportedRequestError(
                f"horizon {overshoot} outside 0..{caps.max_horizon_days} days", source_id=self.source_id
            )

    def policy_url(self, request: CollectionRequest) -> str:
        """URL presented to the policy engine for robots / rate decisions."""
        return self._base_url or f"https://{self.source_id}.invalid/"

    # -- subclass seam ------------------------------------------------------ #
    @abc.abstractmethod
    async def _collect(self, request: CollectionRequest) -> Sequence[FareQuote]:
        """Fetch + parse.  Raise :class:`CollectorError` subclasses on failure."""

    # -- helpers ------------------------------------------------------------ #
    async def _report_response(
        self,
        *,
        url: str,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        body_snippet: str | None = None,
    ) -> None:
        """Feed a transport response back into the policy engine."""
        if self._policy is not None:
            await self._policy.observe_response(
                source_id=self.source_id,
                url=url,
                status_code=status_code,
                headers=headers,
                body_snippet=body_snippet,
            )

    @staticmethod
    def _classify(exc: BaseException) -> tuple[CollectorStatus, str]:
        if getattr(exc, "circuit_open", False):
            return CollectorStatus.CIRCUIT_OPEN, str(exc)
        if getattr(exc, "policy_blocked", False):
            return CollectorStatus.POLICY_BLOCKED, str(exc)
        if isinstance(exc, CollectorError):
            return exc.status, str(exc)
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return CollectorStatus.TRANSPORT_ERROR, f"{type(exc).__name__}: {exc}"
        return CollectorStatus.ERROR, f"{type(exc).__name__}: {exc}"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<{type(self).__name__} source_id={self.source_id!r} type={self.source_type}>"
