"""Deterministic synthetic collector modelling airline yield management.

Why this exists
---------------
A statistical platform must be testable without touching a live carrier.  This
collector reproduces the *shape* of Indian domestic pricing - a steep
last-minute surge, a flat advance-purchase floor, weekday demand peaks, GST
slabs, statutory airport levies - with byte-for-byte reproducibility.  The same
``(seed, route, carrier, flight, departure date, observation date, cabin)`` key
always yields the same fare, so index regressions are detectable.

The booking curve
-----------------
``price = anchor(route, carrier) x yield(T+n) x weekday(dep) x (1 + jitter)``

``yield(T+n)`` is anchored at the five sampled windows and linearly interpolated
across every day in T+1 .. T+45 in ``Decimal``:

===========  ==========  ==================================================
Window       Multiplier  Behaviour
===========  ==========  ==================================================
T+1          2.40        Last-minute surge; inventory nearly exhausted
T+7          1.61        Business booking window
T+15         1.27        Transition band
T+30         1.03        Advance purchase
T+45         0.90        Baseline / promotional floor
===========  ==========  ==================================================

Deliberate data-quality injection
---------------------------------
The generator also emits, deterministically and at configurable rates, the three
pathologies the cleaning pipeline exists to handle:

* ``SOLD_OUT`` rows with **no** price key at all - which must become
  ``total_fare = NULL``, never ``0``, or the Jevons geometric mean collapses;
* all-in rows with **no** component keys - which must become
  ``base_fare = NULL`` plus a ``PARTIAL_BREAKDOWN`` flag, never ``0``;
* extreme price shocks - which the MAD screen must flag without deleting.

Money is emitted as localized strings (``"₹ 4,521.00"``, ``"INR 4521"``) on
purpose, to exercise the currency parser end-to-end.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Final
from collections.abc import Mapping

from apix.collectors.base import (
    BaseCollector,
    CollectionTask,
    ExtractedFare,
    RawCollectorResponse,
)
from apix.collectors.registry import SourceRegistry
from apix.enums import CabinClass, InventoryStatus, PayloadKind, SourceKind

__all__ = [
    "BOOKING_CURVE",
    "BOOKING_CURVE_ANCHORS",
    "MAX_LEAD_DAY",
    "MIN_LEAD_DAY",
    "MockCollector",
    "yield_multiplier",
]

_CENT: Final[Decimal] = Decimal("0.01")
_ONE: Final[Decimal] = Decimal("1")

MIN_LEAD_DAY: Final[int] = 1
MAX_LEAD_DAY: Final[int] = 45

#: Yield multipliers at the five sampled booking windows.
BOOKING_CURVE_ANCHORS: Final[Mapping[int, Decimal]] = MappingProxyType(
    {
        1: Decimal("2.40"),   # T+1  last-minute surge
        7: Decimal("1.61"),   # T+7  business window
        15: Decimal("1.27"),  # T+15 transition
        30: Decimal("1.03"),  # T+30 advance purchase
        45: Decimal("0.90"),  # T+45 baseline
    }
)

#: Intra-window shape: the curve is convex, so a plain straight line between
#: T+1 and T+7 would understate the surge on T+2..T+4.  These extra anchors
#: preserve convexity while keeping the five published windows exact.
_SHAPE_ANCHORS: Final[Mapping[int, Decimal]] = MappingProxyType(
    {2: Decimal("2.18"), 3: Decimal("2.02"), 4: Decimal("1.90"), 5: Decimal("1.79"),
     10: Decimal("1.44"), 21: Decimal("1.14")}
)


def _build_curve() -> Mapping[int, Decimal]:
    """Materialise T+1..T+45 by linear interpolation between all anchors."""
    anchors = dict(BOOKING_CURVE_ANCHORS)
    anchors.update(_SHAPE_ANCHORS)
    ordered = sorted(anchors.items())
    curve: dict[int, Decimal] = {}
    for (lo_day, lo_mult), (hi_day, hi_mult) in zip(ordered, ordered[1:], strict=False):
        span = Decimal(hi_day - lo_day)
        for day in range(lo_day, hi_day):
            step = Decimal(day - lo_day)
            curve[day] = (lo_mult + (hi_mult - lo_mult) * step / span).quantize(Decimal("0.0001"))
    last_day, last_mult = ordered[-1]
    curve[last_day] = last_mult.quantize(Decimal("0.0001"))
    return MappingProxyType(dict(sorted(curve.items())))


#: Fully materialised yield curve, T+1 .. T+45 inclusive.
BOOKING_CURVE: Final[Mapping[int, Decimal]] = _build_curve()

#: Cabin uplift over the economy anchor.
_CABIN_MULTIPLIER: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        CabinClass.ECONOMY: Decimal("1.00"),
        CabinClass.PREMIUM_ECONOMY: Decimal("1.55"),
        CabinClass.BUSINESS: Decimal("2.85"),
        CabinClass.FIRST: Decimal("4.40"),
    }
)

#: Departure-weekday demand factor (Mon=0 .. Sun=6). Friday and Sunday peak.
_WEEKDAY_FACTOR: Final[tuple[Decimal, ...]] = (
    Decimal("1.02"), Decimal("0.97"), Decimal("0.96"), Decimal("1.00"),
    Decimal("1.09"), Decimal("0.99"), Decimal("1.07"),
)

#: GST slabs on Indian domestic air travel, applied to base + carrier charges.
_GST_RATE: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        CabinClass.ECONOMY: Decimal("0.05"),
        CabinClass.PREMIUM_ECONOMY: Decimal("0.05"),
        CabinClass.BUSINESS: Decimal("0.12"),
        CabinClass.FIRST: Decimal("0.12"),
    }
)

#: Aviation Security Fee - a flat statutory levy, identical at every airport.
_ASF: Final[Decimal] = Decimal("236.00")

_CARRIERS: Final[tuple[str, ...]] = ("6E", "AI", "UK", "SG", "QP", "IX")


def yield_multiplier(lead_window_days: int) -> Decimal:
    """Yield multiplier for a horizon, clamped to the modelled T+1..T+45 band."""
    day = min(max(lead_window_days, MIN_LEAD_DAY), MAX_LEAD_DAY)
    return BOOKING_CURVE[day]


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _seed_of(*parts: object) -> int:
    material = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


@SourceRegistry.register(
    "mock_feed",
    kind=SourceKind.MOCK_FEED,
    tags=("synthetic", "ci", "offline"),
    defaults={"base_url": "https://mock.apix.local"},
)
class MockCollector(BaseCollector):
    """Reproducible yield-management simulator implementing the full contract."""

    source_code: ClassVar[str] = "mock_feed"
    kind: ClassVar[str] = SourceKind.MOCK_FEED
    requires_browser: ClassVar[bool] = False
    supported_cabins: ClassVar[frozenset[str]] = frozenset(CabinClass.values)
    max_lead_window_days: ClassVar[int] = MAX_LEAD_DAY

    #: Generation knobs; override per instance from ``Source.attributes``.
    DEFAULTS: ClassVar[dict[str, Any]] = {
        "seed": "apix-mock-v1",
        "carriers": _CARRIERS,
        "flights_per_carrier": 3,
        "anchor_fare_min": Decimal("2800.00"),
        "anchor_fare_max": Decimal("9400.00"),
        "jitter_pct": Decimal("0.06"),
        # Probability the row omits every component key (all-in price only).
        "partial_breakdown_rate": 0.12,
        # Probability a surcharge is genuinely waived and reported as zero.
        "waived_charge_rate": 0.10,
        # Probability the row is SOLD_OUT (rises sharply inside T+3).
        "sold_out_rate": 0.04,
        # Probability of an injected price shock.
        "outlier_rate": 0.01,
        # Probability money is rendered as a localized string, not a number.
        "localized_string_rate": 0.35,
    }

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.cfg: dict[str, Any] = {**self.DEFAULTS, **{k: v for k, v in options.items() if k in self.DEFAULTS}}

    # -- fetch -------------------------------------------------------------- #
    def search_url(self, task: CollectionTask) -> str:
        return (
            f"{self.base_url or 'https://mock.apix.local'}/v1/search"
            f"?o={task.origin}&d={task.destination}&date={task.departure_date.isoformat()}"
            f"&cabin={task.cabin}"
        )

    def fetch(self, task: CollectionTask) -> RawCollectorResponse:
        """Generate a carrier-API-shaped payload for one search."""
        carriers: tuple[str, ...] = tuple(self.cfg["carriers"])
        results: list[dict[str, Any]] = []

        for carrier in carriers:
            for leg in range(1, int(self.cfg["flights_per_carrier"]) + 1):
                results.append(self._make_row(task, carrier, leg))

        payload: dict[str, Any] = {
            "meta": {
                "provider": self.source_code,
                # Derived from the task, never from the wall clock: a `now()`
                # inside the payload would change the ingress hash on every
                # call, defeating the idempotent-ingestion guarantee that
                # `RawObservation.ingress_hash` is unique.  The real capture
                # instant is recorded outside the hashed payload.
                "generated_for": task.observation_date.isoformat(),
                "currency": task.currency,
                "deterministic_seed": self.cfg["seed"],
            },
            "search": {
                "origin": task.origin,
                "destination": task.destination,
                "departure_date": task.departure_date.isoformat(),
                "observation_date": task.observation_date.isoformat(),
                "lead_window_days": task.lead_window_days,
                "cabin": task.cabin,
            },
            "results": results,
        }
        return RawCollectorResponse(
            payload=payload,
            request_url=self.search_url(task),
            payload_kind=PayloadKind.JSON,
            http_status=200,
            response_headers={"content-type": "application/json", "x-apix-synthetic": "1"},
            collector=self.source_code,
        )

    # -- extract ------------------------------------------------------------ #
    def extract(
        self, payload: dict[str, Any] | list[Any], task: CollectionTask
    ) -> list[ExtractedFare]:
        """Map the synthetic payload onto the loosely-typed extraction schema.

        Keys are *omitted*, never zeroed, when the simulated source did not
        publish them - that omission is the signal the cleaning pipeline turns
        into ``NULL``.
        """
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        fares: list[ExtractedFare] = []

        for row in rows:
            fare: ExtractedFare = {
                "carrier_iata": row["carrier"],
                "flight_number": row["flight_number"],
                "cabin": row.get("cabin", task.cabin),
                "departure_local": row["departure_local"],
                "arrival_local": row.get("arrival_local", ""),
                "currency": row.get("currency", task.currency),
                "inventory_status": row.get("inventory_status", InventoryStatus.AVAILABLE),
                "fare_basis": row.get("fare_basis", ""),
                "booking_class": row.get("booking_class", ""),
            }
            for key in ("base_fare", "udf", "psf", "asf", "gst", "other_charges", "total_fare"):
                if key in row["pricing"]:
                    fare[key] = row["pricing"][key]  # type: ignore[literal-required]
            if "seats_remaining" in row:
                fare["seats_remaining"] = row["seats_remaining"]
            if "refundable" in row:
                fare["is_refundable"] = row["refundable"]
            fares.append(fare)

        return fares

    # -- generation --------------------------------------------------------- #
    def _make_row(self, task: CollectionTask, carrier: str, leg: int) -> dict[str, Any]:
        cfg = self.cfg
        flight_number = f"{carrier}{100 + _seed_of(cfg['seed'], 'flt', carrier, task.route_code, leg) % 900}"
        rng = random.Random(
            _seed_of(
                cfg["seed"], self.source_code, task.route_code, carrier, flight_number,
                task.departure_date.isoformat(), task.observation_date.isoformat(), task.cabin,
            )
        )

        dep_time = time(hour=rng.randrange(5, 23), minute=rng.choice((0, 10, 15, 25, 35, 40, 50)))
        dep_local = datetime.combine(task.departure_date, dep_time)
        arr_local = dep_local + timedelta(minutes=rng.randrange(75, 195))

        row: dict[str, Any] = {
            "carrier": carrier,
            "flight_number": flight_number,
            "cabin": task.cabin,
            "departure_local": dep_local.isoformat(),
            "arrival_local": arr_local.isoformat(),
            "currency": task.currency,
            "fare_basis": self._fare_basis(rng, task),
            "booking_class": self._booking_class(task),
            "pricing": {},
        }

        # -- inventory: scarcity intensifies as departure approaches -------- #
        sold_out_probability = float(cfg["sold_out_rate"]) * (4.0 if task.lead_window_days <= 3 else 1.0)
        if rng.random() < sold_out_probability:
            # No price key whatsoever.  The pipeline must store NULL, not 0.
            row["inventory_status"] = InventoryStatus.SOLD_OUT
            row["seats_remaining"] = 0
            return row

        row["inventory_status"] = InventoryStatus.AVAILABLE
        row["seats_remaining"] = self._seats(rng, task.lead_window_days)
        row["refundable"] = task.lead_window_days > 14 and rng.random() < 0.35

        # -- price -------------------------------------------------------- #
        anchor = self._anchor_fare(task.route_code, carrier, task.cabin)
        curve = yield_multiplier(task.lead_window_days)
        weekday = _WEEKDAY_FACTOR[task.departure_date.weekday()]
        jitter = _ONE + (Decimal(str(rng.uniform(-1.0, 1.0))) * Decimal(str(cfg["jitter_pct"]))).quantize(
            Decimal("0.0001")
        )
        base_fare = _money(anchor * curve * weekday * jitter)

        if rng.random() < float(cfg["outlier_rate"]):
            # A real market shock - a fare cap breach or a fat-finger sale.
            base_fare = _money(base_fare * Decimal(str(rng.choice((0.28, 0.34, 3.6, 4.8, 6.2)))))

        other = (
            Decimal("0.00")
            if rng.random() < float(cfg["waived_charge_rate"])
            else _money(Decimal(rng.randrange(150, 900, 25)))
        )
        udf = _money(Decimal(150 + (_seed_of("udf", task.origin) % 12) * 25))
        psf = _money(Decimal(150 + (_seed_of("psf", task.destination) % 8) * 25))
        asf = _ASF
        gst = _money((base_fare + other) * _GST_RATE.get(task.cabin, Decimal("0.05")))
        total = _money(base_fare + other + udf + psf + asf + gst)

        localize = rng.random() < float(cfg["localized_string_rate"])
        fmt = self._localize if localize else (lambda value: str(value))

        if rng.random() < float(cfg["partial_breakdown_rate"]):
            # An OTA that publishes only the all-in price.  Component keys are
            # absent - the auditor must record NULL and flag PARTIAL_BREAKDOWN.
            row["pricing"] = {"total_fare": fmt(total)}
        else:
            row["pricing"] = {
                "base_fare": fmt(base_fare),
                "udf": fmt(udf),
                "psf": fmt(psf),
                "asf": fmt(asf),
                "gst": fmt(gst),
                "other_charges": fmt(other),
                "total_fare": fmt(total),
            }
        return row

    # -- deterministic primitives ------------------------------------------ #
    def _anchor_fare(self, route_code: str, carrier: str, cabin: str) -> Decimal:
        """Stable T+45 economy-equivalent anchor for a route/carrier pair."""
        low = Decimal(str(self.cfg["anchor_fare_min"]))
        high = Decimal(str(self.cfg["anchor_fare_max"]))
        position = Decimal(_seed_of(self.cfg["seed"], "anchor", route_code, carrier) % 10_000) / Decimal(10_000)
        return _money((low + (high - low) * position) * _CABIN_MULTIPLIER.get(cabin, _ONE))

    @staticmethod
    def _localize(value: Decimal) -> str:
        """Render Decimal in one of the formats Indian sites actually emit."""
        digits = f"{value:,.2f}"
        return f"₹ {digits}"

    @staticmethod
    def _seats(rng: random.Random, lead_window_days: int) -> int:
        ceiling = 4 + int(lead_window_days * 0.8)
        return max(1, min(rng.randint(1, ceiling), 60))

    @staticmethod
    def _booking_class(task: CollectionTask) -> str:
        if task.cabin == CabinClass.BUSINESS:
            return "J" if task.lead_window_days <= 7 else "C"
        if task.cabin == CabinClass.FIRST:
            return "F"
        if task.cabin == CabinClass.PREMIUM_ECONOMY:
            return "W"
        if task.lead_window_days <= 3:
            return "Y"
        return "M" if task.lead_window_days <= 14 else "Q"

    @classmethod
    def _fare_basis(cls, rng: random.Random, task: CollectionTask) -> str:
        bucket = cls._booking_class(task)
        return f"{bucket}{min(task.lead_window_days, MAX_LEAD_DAY):02d}{rng.choice('ABCDEFGH')}IN"
