"""Deterministic synthetic collector used for CI, load tests and demos.

The generator is *reproducible*: every quote is derived from a SHA-256 seed over
the semantic key ``(seed, source, route, carrier, flight, departure_date,
observation_date, cabin)``.  The same inputs always yield the same fare, which
makes index regression tests possible without a network.

Pricing model
-------------
``price = anchor(route, carrier) x booking_curve(T-n) x weekday x jitter``

``booking_curve`` is the classic airline yield curve: fares rise steeply inside
the last week before departure and flatten out in the advance-purchase window.
The curve is defined by anchors at T+1 .. T+45 days-to-departure and linearly
interpolated in ``Decimal`` for every day in between, so the table below is the
single source of truth for the simulated yield behaviour.

The collector also injects, deterministically and at configurable rates:

* **missing components** - a source that publishes only an all-in price, which
  exercises the "Missing != Zero" auditor in ``app.cleaning.normalizer``;
* **explicit zeros** - a genuinely waived surcharge, which must survive as
  ``Decimal("0")`` and never be confused with the case above;
* **price outliers** - to exercise the MAD detector in ``app.cleaning.outliers``.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from app.collectors.base import (
    BaseCollector,
    CabinClass,
    CollectionRequest,
    CollectorCapabilities,
    FareQuote,
    PolicyGuard,
    SourceType,
    payload_digest,
)
from app.collectors.registry import SourceRegistry

__all__ = [
    "BOOKING_CURVE",
    "BOOKING_CURVE_ANCHORS",
    "MAX_CURVE_DAY",
    "MIN_CURVE_DAY",
    "MockCollector",
    "MockCollectorConfig",
    "booking_curve_multiplier",
]

_UTC: Final = timezone.utc
_CENT: Final[Decimal] = Decimal("0.01")
_ONE: Final[Decimal] = Decimal("1")

MIN_CURVE_DAY: Final[int] = 1
MAX_CURVE_DAY: Final[int] = 45

#: Yield multipliers at anchor points, expressed in days-to-departure (T+n).
#: T+1 = departing tomorrow (peak yield); T+45 = deep advance purchase.
BOOKING_CURVE_ANCHORS: Final[Mapping[int, Decimal]] = MappingProxyType(
    {
        1: Decimal("2.40"),
        2: Decimal("2.18"),
        3: Decimal("2.02"),
        4: Decimal("1.90"),
        5: Decimal("1.79"),
        7: Decimal("1.61"),
        10: Decimal("1.44"),
        14: Decimal("1.29"),
        21: Decimal("1.14"),
        30: Decimal("1.03"),
        45: Decimal("0.90"),
    }
)


def _build_curve() -> Mapping[int, Decimal]:
    """Materialise T+1..T+45 by linear interpolation between the anchors."""
    anchors = sorted(BOOKING_CURVE_ANCHORS.items())
    curve: dict[int, Decimal] = {}
    for (lo_day, lo_mult), (hi_day, hi_mult) in zip(anchors, anchors[1:], strict=False):
        span = Decimal(hi_day - lo_day)
        for day in range(lo_day, hi_day):
            step = Decimal(day - lo_day)
            curve[day] = (lo_mult + (hi_mult - lo_mult) * step / span).quantize(Decimal("0.0001"))
    last_day, last_mult = anchors[-1]
    curve[last_day] = last_mult.quantize(Decimal("0.0001"))
    return MappingProxyType(dict(sorted(curve.items())))


#: Fully materialised yield curve, T+1 .. T+45 inclusive.
BOOKING_CURVE: Final[Mapping[int, Decimal]] = _build_curve()

#: Multiplicative cabin uplift over the economy anchor fare.
_CABIN_MULTIPLIER: Final[Mapping[CabinClass, Decimal]] = MappingProxyType(
    {
        CabinClass.ECONOMY: Decimal("1.00"),
        CabinClass.PREMIUM_ECONOMY: Decimal("1.55"),
        CabinClass.BUSINESS: Decimal("2.85"),
        CabinClass.FIRST: Decimal("4.40"),
    }
)

#: Departure-weekday demand factor (Mon=0 .. Sun=6): Friday/Sunday peaks.
_WEEKDAY_FACTOR: Final[tuple[Decimal, ...]] = (
    Decimal("1.02"),
    Decimal("0.97"),
    Decimal("0.96"),
    Decimal("1.00"),
    Decimal("1.09"),
    Decimal("0.99"),
    Decimal("1.07"),
)

#: Indian GST slabs applied to (base + surcharges).
_GST_RATE: Final[Mapping[CabinClass, Decimal]] = MappingProxyType(
    {
        CabinClass.ECONOMY: Decimal("0.05"),
        CabinClass.PREMIUM_ECONOMY: Decimal("0.05"),
        CabinClass.BUSINESS: Decimal("0.12"),
        CabinClass.FIRST: Decimal("0.12"),
    }
)


def booking_curve_multiplier(days_to_departure: int) -> Decimal:
    """Yield multiplier for a horizon, clamped to the modelled T+1..T+45 band."""
    day = min(max(days_to_departure, MIN_CURVE_DAY), MAX_CURVE_DAY)
    return BOOKING_CURVE[day]


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _seed_of(*parts: object) -> int:
    """Stable 64-bit seed from the semantic key of a quote."""
    material = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


def _rng(*parts: object) -> random.Random:
    return random.Random(_seed_of(*parts))


class MockCollectorConfig(BaseModel):
    """Tuning knobs for the synthetic generator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: str = "apix-mock-v1"
    carriers: tuple[str, ...] = ("6E", "AI", "UK", "SG", "QP")
    flights_per_carrier: int = Field(default=3, ge=1, le=20)
    anchor_fare_min: Decimal = Field(default=Decimal("2800.00"), gt=Decimal("0"))
    anchor_fare_max: Decimal = Field(default=Decimal("9400.00"), gt=Decimal("0"))
    jitter_pct: Decimal = Field(default=Decimal("0.06"), ge=Decimal("0"), le=Decimal("0.5"))
    #: Probability a quote publishes only an all-in fare (components unknown).
    missing_component_rate: float = Field(default=0.08, ge=0.0, le=1.0)
    #: Probability a surcharge is genuinely waived and reported as zero.
    waived_surcharge_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    #: Probability of an injected price outlier.
    outlier_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    #: Simulated round-trip latency per departure date.
    latency_ms: int = Field(default=0, ge=0, le=5_000)


@SourceRegistry.register(
    "mock",
    source_type=SourceType.MOCK,
    tags=("synthetic", "ci", "offline"),
    defaults={"base_url": "https://mock.apix.local/"},
)
class MockCollector(BaseCollector):
    """Reproducible fare generator implementing the full collector contract."""

    source_id: ClassVar[str] = "mock"
    source_type: ClassVar[SourceType] = SourceType.MOCK
    capabilities: ClassVar[CollectorCapabilities] = CollectorCapabilities(
        supports_cabins=frozenset(CabinClass),
        max_horizon_days=MAX_CURVE_DAY,
        supports_one_way=True,
        supports_seat_availability=True,
        supports_fare_breakdown=True,
        max_concurrency=8,
        requires_robots_check=False,
        currencies=frozenset({"INR"}),
    )

    def __init__(
        self,
        *,
        policy: PolicyGuard | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        config: MockCollectorConfig | None = None,
    ) -> None:
        super().__init__(policy=policy, base_url=base_url, timeout_s=timeout_s)
        self._config = config or MockCollectorConfig()

    @property
    def config(self) -> MockCollectorConfig:
        return self._config

    # -- generation --------------------------------------------------------- #
    async def _collect(self, request: CollectionRequest) -> Sequence[FareQuote]:
        observation_ts = datetime.now(_UTC)
        carriers = request.carriers or self._config.carriers
        quotes: list[FareQuote] = []

        for departure in request.departure_dates:
            if self._config.latency_ms:
                await asyncio.sleep(self._config.latency_ms / 1000)
            days_to_departure = (departure - request.observation_date).days
            for carrier in carriers:
                for leg in range(1, self._config.flights_per_carrier + 1):
                    quotes.append(
                        self._make_quote(
                            request=request,
                            carrier=carrier,
                            leg=leg,
                            departure=departure,
                            days_to_departure=days_to_departure,
                            observation_ts=observation_ts,
                        )
                    )
        return quotes

    def _make_quote(
        self,
        *,
        request: CollectionRequest,
        carrier: str,
        leg: int,
        departure: date,
        days_to_departure: int,
        observation_ts: datetime,
    ) -> FareQuote:
        cfg = self._config
        flight_number = f"{carrier}{self._flight_digits(carrier, request.route, leg)}"
        rng = _rng(
            cfg.seed,
            self.source_id,
            request.route,
            carrier,
            flight_number,
            departure.isoformat(),
            request.observation_date.isoformat(),
            request.cabin,
        )

        anchor = self._anchor_fare(request.route, carrier, request.cabin)
        curve = booking_curve_multiplier(days_to_departure)
        weekday = _WEEKDAY_FACTOR[departure.weekday()]
        jitter = _ONE + (Decimal(str(rng.uniform(-1.0, 1.0))) * cfg.jitter_pct).quantize(Decimal("0.0001"))

        base_fare = _money(anchor * curve * weekday * jitter)

        is_outlier = rng.random() < cfg.outlier_rate
        if is_outlier:
            shock = Decimal(str(rng.choice((0.28, 0.34, 3.6, 4.8, 6.2))))
            base_fare = _money(base_fare * shock)

        surcharges = (
            Decimal("0.00")
            if rng.random() < cfg.waived_surcharge_rate
            else _money(Decimal(rng.randrange(150, 900, 25)))
        )
        fees = _money(self._airport_fees(request.origin, request.destination))
        taxes = _money((base_fare + surcharges) * _GST_RATE[request.cabin])
        total = _money(base_fare + surcharges + fees + taxes)

        # Some sources publish only the all-in price: components are unknown,
        # which is categorically different from a component that is zero.
        components_published = rng.random() >= cfg.missing_component_rate

        departure_time = f"{rng.randrange(5, 23):02d}:{rng.choice((0, 10, 15, 25, 35, 40, 50)):02d}"
        seats_remaining = self._seat_pressure(rng, days_to_departure)

        raw_payload = (
            f"{self.source_id}|{flight_number}|{request.route}|{departure.isoformat()}"
            f"|{request.cabin}|{total}|{request.currency}|{departure_time}"
        )

        return FareQuote(
            source_id=self.source_id,
            source_type=self.source_type,
            carrier_code=carrier,
            flight_number=flight_number,
            origin=request.origin,
            destination=request.destination,
            departure_date=departure,
            departure_time=departure_time,
            observation_ts=observation_ts,
            days_to_departure=max(days_to_departure, 0),
            cabin=request.cabin,
            currency=request.currency,
            base_fare=base_fare if components_published else None,
            taxes=taxes if components_published else None,
            fees=fees if components_published else None,
            surcharges=surcharges if components_published else None,
            total_fare=total,
            seats_remaining=seats_remaining,
            fare_basis=self._fare_basis(rng, request.cabin, days_to_departure),
            booking_class=self._booking_class(request.cabin, days_to_departure),
            is_refundable=days_to_departure > 14 and rng.random() < 0.35,
            raw_digest=payload_digest(raw_payload),
            attributes={
                "generator": "mock",
                "curve_multiplier": str(curve),
                "weekday_factor": str(weekday),
                "synthetic_outlier": str(is_outlier).lower(),
                "components_published": str(components_published).lower(),
            },
        )

    # -- deterministic primitives ------------------------------------------- #
    def _anchor_fare(self, route: str, carrier: str, cabin: CabinClass) -> Decimal:
        """Stable T+45 economy-equivalent anchor for a route/carrier pair."""
        cfg = self._config
        spread = cfg.anchor_fare_max - cfg.anchor_fare_min
        position = Decimal(_seed_of(cfg.seed, "anchor", route, carrier) % 10_000) / Decimal(10_000)
        economy = cfg.anchor_fare_min + spread * position
        return _money(economy * _CABIN_MULTIPLIER[cabin])

    def _flight_digits(self, carrier: str, route: str, leg: int) -> int:
        return 100 + (_seed_of(self._config.seed, "flight", carrier, route, leg) % 900)

    def _airport_fees(self, origin: str, destination: str) -> Decimal:
        """UDF/PSF style fixed levies, stable per station pair."""
        udf = Decimal(150 + (_seed_of("udf", origin) % 12) * 25)
        psf = Decimal(150 + (_seed_of("psf", destination) % 8) * 25)
        return udf + psf

    @staticmethod
    def _seat_pressure(rng: random.Random, days_to_departure: int) -> int:
        """Inventory shrinks as departure approaches - mirrors the yield curve."""
        ceiling = 4 + int(days_to_departure * 0.8)
        return max(1, min(rng.randint(1, ceiling), 60))

    @staticmethod
    def _booking_class(cabin: CabinClass, days_to_departure: int) -> str:
        if cabin is CabinClass.BUSINESS:
            return "J" if days_to_departure <= 7 else "C"
        if cabin is CabinClass.FIRST:
            return "F"
        if cabin is CabinClass.PREMIUM_ECONOMY:
            return "W"
        if days_to_departure <= 3:
            return "Y"
        if days_to_departure <= 14:
            return "M"
        return "Q"

    @classmethod
    def _fare_basis(cls, rng: random.Random, cabin: CabinClass, days_to_departure: int) -> str:
        bucket = cls._booking_class(cabin, days_to_departure)
        return f"{bucket}{min(days_to_departure, MAX_CURVE_DAY):02d}{rng.choice('ABCDEFGH')}IN"
