"""Statistical compilation: Jevons elementary aggregates, DGCA Laspeyres, sealing.

Two stages, because they answer different questions and have different data.

Stage 1 - elementary aggregate (within a route x lead-window cell): **Jevons**
--------------------------------------------------------------------------
Inside a cell we have matched price pairs and no reliable transaction
quantities, which is precisely the case the Jevons index is designed for: the
unweighted geometric mean of price relatives.

.. math::

    I_{J} = 100 \\times \\prod_{i=1}^{n}
            \\left(\\frac{p_{t,i}}{p_{0,i}}\\right)^{1/n}
          = 100 \\times \\exp\\!\\left(\\frac{1}{n}
            \\sum_{i=1}^{n} \\ln \\frac{p_{t,i}}{p_{0,i}}\\right)

It is evaluated in the **log-space** form on the right, never as a product of
relatives.  Three reasons:

1. *Overflow.*  With a few hundred matched pairs, the running product of
   relatives overflows or underflows long before the ``1/n`` root is taken.
2. *Precision.*  Repeated multiplication of ``Decimal`` values compounds
   rounding; a sum of logarithms accumulates error linearly, not
   multiplicatively.
3. *Trimming.*  Symmetric trimming of extreme relatives is a slice on a sorted
   list of logs - trivial in log space, awkward as a product.

The Jevons form also embodies the right economic assumption for airfares: it is
exact for a unitary price elasticity of substitution, i.e. passengers who
substitute between flights when relative prices move.  A Dutot (ratio of mean
prices) would let one expensive business-class cell dominate the cell average.

Stage 2 - higher-level aggregate (across routes): **Laspeyres**
--------------------------------------------------------------
DGCA publishes passenger traffic with a lag, so current-period quantities do not
exist when the index is compiled.  Fixing quantities to a base period is exactly
the Laspeyres form:

.. math::

    I_{L} = \\frac{\\sum_r p_{t,r}\\,q_{0,r}}{\\sum_r p_{0,r}\\,q_{0,r}}
          = \\sum_r w_{0,r} \\, I_{J,r},
    \\qquad w_{0,r} = \\frac{p_{0,r} q_{0,r}}{\\sum_s p_{0,s} q_{0,s}}

- a base-weighted arithmetic mean of the elementary indices, where
:math:`w_{0,r}` is route *r*'s DGCA passenger share.  Routes with no usable data
are dropped and the surviving shares renormalised; the resulting
``weight_coverage`` is published with the level, because an index computed on
55% of the weight base must say so rather than pretend to be national.

All arithmetic is :class:`decimal.Decimal` with a 34-digit context and banker's
rounding, including every logarithm and exponential.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Final
from collections.abc import Iterable, Mapping, Sequence

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apix.enums import AuditAction
from apix.models import AuditLog, CanonicalFare, IndexValue, Route, RouteWeight
from apix.services.provenance import ProvenanceSealer, sha256_hex

__all__ = [
    "ElementaryResult",
    "IndexCompiler",
    "JevonsCalculator",
    "LaspeyresAggregator",
    "MatchedPair",
    "compile_index_for_date",
]

logger = logging.getLogger(__name__)

_ZERO: Final[Decimal] = Decimal("0")
_HUNDRED: Final[Decimal] = Decimal("100")

#: 34 significant digits: far beyond IEEE-754 double, so summing thousands of
#: logarithms never costs a basis point on the published level.
_CTX: Final[Context] = Context(prec=34, rounding=ROUND_HALF_EVEN)
_LEVEL_QUANT: Final[Decimal] = Decimal("0.00000001")
_WEIGHT_QUANT: Final[Decimal] = Decimal("0.0000000001")
_LOG_QUANT: Final[Decimal] = Decimal("0.000000000001")


# --------------------------------------------------------------------------- #
# Elementary aggregate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MatchedPair:
    """The same flight product priced in the base period and the current one."""

    item_key: str
    base_price: Decimal
    current_price: Decimal

    @property
    def relative(self) -> Decimal:
        with localcontext(_CTX):
            return self.current_price / self.base_price


@dataclass(frozen=True, slots=True)
class ElementaryResult:
    index_value: Decimal
    mean_log_relative: Decimal
    pairs_used: int
    pairs_trimmed: int = 0
    pairs_rejected: int = 0

    @property
    def is_computable(self) -> bool:
        return self.pairs_used > 0 and self.index_value > _ZERO


class JevonsCalculator:
    """Geometric mean of price relatives, evaluated in log space."""

    def __init__(
        self,
        *,
        base_level: Decimal = _HUNDRED,
        trim_fraction: Decimal = Decimal("0.02"),
        min_pairs: int = 5,
    ) -> None:
        self.base_level = base_level
        self.trim_fraction = trim_fraction
        self.min_pairs = min_pairs

    def elementary_index(self, pairs: Sequence[MatchedPair]) -> ElementaryResult:
        """Compute ``100 * exp(mean(ln(p_t / p_0)))`` for one cell."""
        usable = [
            pair for pair in pairs
            if pair.base_price > _ZERO and pair.current_price > _ZERO
        ]
        rejected = len(pairs) - len(usable)
        if not usable:
            return ElementaryResult(_ZERO, _ZERO, 0, 0, rejected)

        with localcontext(_CTX):
            # ln(p_t/p_0) once per pair.  No running product exists at any point.
            logs = sorted((pair.current_price / pair.base_price).ln() for pair in usable)
            kept, trimmed = self._trim(logs)
            mean_log = sum(kept, _ZERO) / Decimal(len(kept))
            level = (self.base_level * mean_log.exp()).quantize(_LEVEL_QUANT)

        return ElementaryResult(
            index_value=level,
            mean_log_relative=mean_log.quantize(_LOG_QUANT),
            pairs_used=len(kept),
            pairs_trimmed=trimmed,
            pairs_rejected=rejected,
        )

    def geometric_mean(self, values: Sequence[Decimal]) -> Decimal:
        """``exp(mean(ln(v)))`` - used to average base-period prices per item."""
        positive = [value for value in values if value > _ZERO]
        if not positive:
            raise ValueError("geometric mean requires at least one positive value")
        with localcontext(_CTX):
            return (sum((value.ln() for value in positive), _ZERO) / Decimal(len(positive))).exp()

    def _trim(self, ordered_logs: list[Decimal]) -> tuple[list[Decimal], int]:
        """Symmetric trim of the most extreme log-relatives.

        The MAD screen in the cleaning stage already removed the pathological
        cases; this is a second, gentler guard against a legitimately extreme but
        unrepresentative relative dominating a thin cell.
        """
        if self.trim_fraction <= _ZERO or len(ordered_logs) < self.min_pairs:
            return ordered_logs, 0
        cut = int(Decimal(len(ordered_logs)) * self.trim_fraction)
        if cut == 0 or len(ordered_logs) - 2 * cut < 1:
            return ordered_logs, 0
        return ordered_logs[cut: len(ordered_logs) - cut], 2 * cut


# --------------------------------------------------------------------------- #
# Higher-level aggregate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RouteContribution:
    route_code: str
    index_value: Decimal
    raw_share: Decimal
    effective_weight: Decimal
    contribution: Decimal
    included: bool
    exclusion_reason: str = ""


@dataclass(frozen=True, slots=True)
class AggregateResult:
    index_value: Decimal
    weight_coverage: Decimal
    contributions: tuple[RouteContribution, ...]
    weight_set_digest: str

    @property
    def included_routes(self) -> int:
        return sum(1 for c in self.contributions if c.included)


class LaspeyresAggregator:
    """Base-weighted aggregation of elementary indices across the basket."""

    def __init__(self, *, min_weight_coverage: Decimal = Decimal("0.60")) -> None:
        self.min_weight_coverage = min_weight_coverage

    def aggregate(
        self,
        elementary: Mapping[str, ElementaryResult],
        shares: Mapping[str, Decimal],
        *,
        weight_set_digest: str = "",
    ) -> AggregateResult:
        """Combine route indices with DGCA passenger shares.

        Missing routes are excluded and the surviving weights renormalised, so
        the headline is a true weighted mean of what was observed rather than a
        silently zero-filled one.
        """
        rows: list[RouteContribution] = []
        included_weight = _ZERO

        for route_code, share in sorted(shares.items()):
            result = elementary.get(route_code)
            if result is None or not result.is_computable:
                rows.append(RouteContribution(
                    route_code=route_code,
                    index_value=result.index_value if result else _ZERO,
                    raw_share=share,
                    effective_weight=_ZERO,
                    contribution=_ZERO,
                    included=False,
                    exclusion_reason=(
                        "no matched pairs" if result is None
                        else f"only {result.pairs_used} matched pairs"
                    ),
                ))
                continue
            included_weight += share
            rows.append(RouteContribution(
                route_code=route_code,
                index_value=result.index_value,
                raw_share=share,
                effective_weight=_ZERO,
                contribution=_ZERO,
                included=True,
            ))

        if included_weight <= _ZERO:
            return AggregateResult(_ZERO, _ZERO, tuple(rows), weight_set_digest)

        with localcontext(_CTX):
            finalised: list[RouteContribution] = []
            headline = _ZERO
            for row in rows:
                if not row.included:
                    finalised.append(row)
                    continue
                effective = (row.raw_share / included_weight).quantize(_WEIGHT_QUANT)
                contribution = (effective * row.index_value).quantize(_LEVEL_QUANT)
                headline += effective * row.index_value
                finalised.append(RouteContribution(
                    route_code=row.route_code,
                    index_value=row.index_value,
                    raw_share=row.raw_share,
                    effective_weight=effective,
                    contribution=contribution,
                    included=True,
                ))
            return AggregateResult(
                index_value=headline.quantize(_LEVEL_QUANT),
                weight_coverage=included_weight.quantize(_WEIGHT_QUANT),
                contributions=tuple(finalised),
                weight_set_digest=weight_set_digest,
            )

    def laspeyres_from_prices(
        self,
        *,
        base_prices: Mapping[str, Decimal],
        current_prices: Mapping[str, Decimal],
        base_quantities: Mapping[str, Decimal],
        base_level: Decimal = _HUNDRED,
    ) -> Decimal:
        """Direct form ``100 * SUM(p_t q_0) / SUM(p_0 q_0)``.

        Algebraically identical to the weighted mean above; computed
        independently as a published cross-check that the weight
        renormalisation did not drift.
        """
        common = sorted(set(base_prices) & set(current_prices) & set(base_quantities))
        if not common:
            raise ValueError("no overlapping items between prices and base quantities")
        with localcontext(_CTX):
            numerator = sum((current_prices[k] * base_quantities[k] for k in common), _ZERO)
            denominator = sum((base_prices[k] * base_quantities[k] for k in common), _ZERO)
            if denominator <= _ZERO:
                raise ValueError("base-period expenditure is zero; Laspeyres undefined")
            return (base_level * numerator / denominator).quantize(_LEVEL_QUANT)


def chain_link(previous_level: Decimal, current_relative: Decimal) -> Decimal:
    """Chain a period-on-period relative onto a published level."""
    if previous_level <= _ZERO or current_relative <= _ZERO:
        raise ValueError("chain linking requires a positive level and relative")
    with localcontext(_CTX):
        return (previous_level * current_relative).quantize(_LEVEL_QUANT)


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #
@dataclass
class CompilationOutcome:
    index_date: date
    lead_window_days: int | None
    route_values: list[IndexValue] = field(default_factory=list)
    national_value: IndexValue | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def published(self) -> int:
        return sum(1 for value in [*self.route_values, self.national_value] if value and value.is_published)


class IndexCompiler:
    """Compiles, seals and stores one day's index for one lead window.

    The matched item is ``carrier|flight_number|cabin`` inside a route and lead
    window: the *same seat product* priced in the base period and again today.
    A flight that did not exist in the base period cannot contribute a relative
    and is counted as unmatched rather than spliced in at an arbitrary level.
    """

    def __init__(
        self,
        *,
        methodology_code: str | None = None,
        base_period: str | None = None,
        base_level: Decimal | None = None,
        trim_fraction: Decimal | None = None,
        min_pairs: int | None = None,
        min_weight_coverage: Decimal | None = None,
    ) -> None:
        cfg = settings.APIX
        self.methodology_code = methodology_code or cfg["METHODOLOGY_CODE"]
        self.base_period = base_period or cfg["BASE_PERIOD"]
        self.base_level = Decimal(str(base_level if base_level is not None else cfg["BASE_INDEX_LEVEL"]))
        self.min_pairs = int(min_pairs if min_pairs is not None else cfg["MIN_PAIRS_PER_STRATUM"])
        self.min_weight_coverage = Decimal(
            str(min_weight_coverage if min_weight_coverage is not None else cfg["MIN_WEIGHT_COVERAGE"])
        )
        self.jevons = JevonsCalculator(
            base_level=self.base_level,
            trim_fraction=Decimal(str(trim_fraction if trim_fraction is not None else cfg["TRIM_FRACTION"])),
            min_pairs=self.min_pairs,
        )
        self.laspeyres = LaspeyresAggregator(min_weight_coverage=self.min_weight_coverage)
        self.sealer = ProvenanceSealer(
            methodology_code=self.methodology_code,
            methodology_digest=self.methodology_digest,
        )

    # -- methodology identity ---------------------------------------------- #
    @property
    def methodology_digest(self) -> str:
        """Hash of every parameter that can move a published level."""
        return sha256_hex({
            "methodology_code": self.methodology_code,
            "base_period": self.base_period,
            "base_level": self.base_level,
            "elementary_formula": "jevons_log_space",
            "aggregation_formula": "laspeyres_dgca_pax_share",
            "trim_fraction": self.jevons.trim_fraction,
            "min_pairs": self.min_pairs,
            "min_weight_coverage": self.min_weight_coverage,
        })

    # -- entry point -------------------------------------------------------- #
    @transaction.atomic
    def compile(self, index_date: date, lead_window_days: int) -> CompilationOutcome:
        outcome = CompilationOutcome(index_date=index_date, lead_window_days=lead_window_days)
        routes = list(Route.objects.in_basket())
        shares = self._weight_shares(routes, on=index_date)
        weight_digest = sha256_hex({"as_of": index_date, "shares": {k: v for k, v in sorted(shares.items())}})

        elementary: dict[str, ElementaryResult] = {}
        ingress_by_route: dict[str, set[str]] = {}
        observation_counts: dict[str, int] = {}

        for route in routes:
            pairs, ingress, n_obs = self._matched_pairs(route, index_date, lead_window_days)
            observation_counts[route.code] = n_obs
            ingress_by_route[route.code] = ingress
            result = self.jevons.elementary_index(pairs)
            if result.pairs_used < self.min_pairs:
                result = ElementaryResult(_ZERO, _ZERO, result.pairs_used, result.pairs_trimmed, result.pairs_rejected)
            elementary[route.code] = result

            value = self._store_route_value(
                route=route,
                index_date=index_date,
                lead_window_days=lead_window_days,
                result=result,
                observations=n_obs,
                ingress_hashes=ingress,
            )
            if value is not None:
                outcome.route_values.append(value)

        aggregate = self.laspeyres.aggregate(elementary, shares, weight_set_digest=weight_digest)
        outcome.national_value = self._store_national_value(
            index_date=index_date,
            lead_window_days=lead_window_days,
            aggregate=aggregate,
            elementary=elementary,
            observations=sum(observation_counts.values()),
            ingress_hashes={h for hashes in ingress_by_route.values() for h in hashes},
        )
        outcome.diagnostics = {
            "routes_evaluated": len(routes),
            "routes_included": aggregate.included_routes,
            "weight_coverage": str(aggregate.weight_coverage),
            "weight_set_digest": weight_digest,
            "observations": observation_counts,
            "elementary": {
                code: {
                    "index_value": str(result.index_value),
                    "pairs_used": result.pairs_used,
                    "pairs_trimmed": result.pairs_trimmed,
                }
                for code, result in elementary.items()
            },
        }
        return outcome

    # -- weights ------------------------------------------------------------ #
    @staticmethod
    def _weight_shares(routes: Sequence[Route], *, on: date) -> dict[str, Decimal]:
        """DGCA shares in force on ``on``, renormalised over the basket."""
        weights = (
            RouteWeight.objects.effective_on(on)
            .filter(route__in=routes)
            .select_related("route")
        )
        raw = {weight.route.code: Decimal(weight.share) for weight in weights}
        total = sum(raw.values(), _ZERO)
        if total <= _ZERO:
            # No weight version in force: fall back to equal weighting and say so
            # loudly rather than silently publishing a mis-weighted index.
            logger.error("no DGCA weights effective", extra={"as_of": on.isoformat()})
            if not routes:
                return {}
            equal = (Decimal(1) / Decimal(len(routes))).quantize(_WEIGHT_QUANT)
            return {route.code: equal for route in routes}
        with localcontext(_CTX):
            return {code: (share / total).quantize(_WEIGHT_QUANT) for code, share in raw.items()}

    # -- matched pairs ------------------------------------------------------ #
    def _matched_pairs(
        self, route: Route, index_date: date, lead_window_days: int
    ) -> tuple[list[MatchedPair], set[str], int]:
        """Build ``(pairs, ingress hashes, observation count)`` for one cell."""
        current = self._prices_for_day(route, index_date, lead_window_days)
        base = self._base_prices(route, lead_window_days)

        pairs = [
            MatchedPair(item_key=item, base_price=base[item], current_price=price)
            for item, price in sorted(current.prices.items())
            if item in base and base[item] > _ZERO
        ]
        return pairs, current.ingress_hashes, current.observations

    @dataclass(frozen=True, slots=True)
    class _PriceSet:
        prices: dict[str, Decimal]
        ingress_hashes: set[str]
        observations: int

    def _prices_for_day(
        self, route: Route, day: date, lead_window_days: int
    ) -> IndexCompiler._PriceSet:
        """Index-eligible fares for one cell, geometrically averaged per item.

        ``index_eligible()`` already excludes duplicates, outliers, modelled
        rows and - critically - every ``total_fare IS NULL`` row, so sold-out
        inventory can never reach the geometric mean.
        """
        rows = (
            CanonicalFare.objects.index_eligible()
            .filter(route=route, observation_date=day, lead_window_days=lead_window_days)
            .select_related("raw_observation")
            .only("carrier_iata", "flight_number", "cabin", "total_fare",
                  "raw_observation__ingress_hash")
        )
        buckets: dict[str, list[Decimal]] = defaultdict(list)
        ingress: set[str] = set()
        count = 0
        for row in rows:
            buckets[self._item_key(row)].append(Decimal(row.total_fare))
            ingress.add(row.raw_observation.ingress_hash)
            count += 1

        prices = {
            item: self.jevons.geometric_mean(values).quantize(Decimal("0.01"))
            for item, values in buckets.items()
        }
        return self._PriceSet(prices=prices, ingress_hashes=ingress, observations=count)

    def _base_prices(self, route: Route, lead_window_days: int) -> dict[str, Decimal]:
        """Base-period reference price per item.

        The base is a *period*, not a day, so each item's base price is the
        geometric mean of every eligible observation of that item during the
        base month.  Using the same (geometric) average at both ends keeps the
        relative unbiased.
        """
        start, end = self._base_period_bounds()
        rows = (
            CanonicalFare.objects.index_eligible()
            .filter(route=route, lead_window_days=lead_window_days,
                    observation_date__gte=start, observation_date__lt=end)
            .only("carrier_iata", "flight_number", "cabin", "total_fare")
        )
        buckets: dict[str, list[Decimal]] = defaultdict(list)
        for row in rows:
            buckets[self._item_key(row)].append(Decimal(row.total_fare))
        return {
            item: self.jevons.geometric_mean(values).quantize(Decimal("0.01"))
            for item, values in buckets.items()
        }

    def _base_period_bounds(self) -> tuple[date, date]:
        year, month = (int(part) for part in self.base_period.split("-")[:2])
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1)
        return start, end

    @staticmethod
    def _item_key(row: CanonicalFare) -> str:
        """A matched *item* is one seat product: carrier, flight, cabin."""
        return f"{row.carrier_iata}|{row.flight_number}|{row.cabin}"

    # -- persistence + sealing ---------------------------------------------- #
    def _store_route_value(
        self,
        *,
        route: Route,
        index_date: date,
        lead_window_days: int,
        result: ElementaryResult,
        observations: int,
        ingress_hashes: set[str],
    ) -> IndexValue | None:
        if not result.is_computable:
            logger.info(
                "route index suppressed",
                extra={"route": route.code, "window": lead_window_days,
                       "pairs": result.pairs_used, "date": index_date.isoformat()},
            )
            return None

        previous = self._previous_hash(route=route, lead_window_days=lead_window_days, before=index_date)
        seal = self.sealer.seal(
            index_date=index_date,
            index_value=result.index_value,
            scope=route.code,
            lead_window_days=lead_window_days,
            base_period=self.base_period,
            ingress_hashes=ingress_hashes,
            previous_hash=previous,
            diagnostics={
                "pairs_used": result.pairs_used,
                "pairs_trimmed": result.pairs_trimmed,
                "observations": observations,
            },
        )
        return self._upsert(
            route=route,
            index_date=index_date,
            lead_window_days=lead_window_days,
            index_value=result.index_value,
            mean_log_relative=result.mean_log_relative,
            n_observations=observations,
            n_matched_pairs=result.pairs_used,
            n_routes=1,
            weight_coverage=None,
            seal=seal,
            is_published=True,
            suppression_reason="",
            metadata={"seal": self._seal_body(seal), "elementary": "jevons_log_space"},
        )

    def _store_national_value(
        self,
        *,
        index_date: date,
        lead_window_days: int,
        aggregate: AggregateResult,
        elementary: Mapping[str, ElementaryResult],
        observations: int,
        ingress_hashes: set[str],
    ) -> IndexValue | None:
        publishable = (
            aggregate.index_value > _ZERO
            and aggregate.weight_coverage >= self.min_weight_coverage
        )
        suppression = "" if publishable else (
            f"weight coverage {aggregate.weight_coverage} below minimum {self.min_weight_coverage}"
            if aggregate.index_value > _ZERO else "no route met the minimum matched-pair requirement"
        )

        previous = self._previous_hash(route=None, lead_window_days=lead_window_days, before=index_date)
        seal = self.sealer.seal(
            index_date=index_date,
            index_value=aggregate.index_value,
            scope="NATIONAL",
            lead_window_days=lead_window_days,
            base_period=self.base_period,
            ingress_hashes=ingress_hashes,
            previous_hash=previous,
            weight_set_digest=aggregate.weight_set_digest,
            diagnostics={
                "routes_included": aggregate.included_routes,
                "weight_coverage": aggregate.weight_coverage,
                "matched_pairs": sum(r.pairs_used for r in elementary.values()),
            },
        )
        if not publishable:
            AuditLog.record(
                AuditAction.INDEX_SUPPRESSED,
                summary=f"NATIONAL T+{lead_window_days} {index_date}: {suppression}",
                context={"weight_coverage": str(aggregate.weight_coverage)},
            )

        return self._upsert(
            route=None,
            index_date=index_date,
            lead_window_days=lead_window_days,
            index_value=aggregate.index_value if aggregate.index_value > _ZERO else Decimal("0.00000001"),
            mean_log_relative=None,
            n_observations=observations,
            n_matched_pairs=sum(r.pairs_used for r in elementary.values()),
            n_routes=aggregate.included_routes,
            weight_coverage=aggregate.weight_coverage,
            seal=seal,
            is_published=publishable,
            suppression_reason=suppression,
            metadata={
                "seal": self._seal_body(seal),
                "aggregation": "laspeyres_dgca_pax_share",
                "contributions": [
                    {
                        "route": contribution.route_code,
                        "index_value": str(contribution.index_value),
                        "raw_share": str(contribution.raw_share),
                        "effective_weight": str(contribution.effective_weight),
                        "contribution": str(contribution.contribution),
                        "included": contribution.included,
                        "exclusion_reason": contribution.exclusion_reason,
                    }
                    for contribution in aggregate.contributions
                ],
            },
        )

    def _upsert(self, *, route: Route | None, index_date: date, lead_window_days: int, **fields: Any) -> IndexValue:
        seal = fields.pop("seal")
        metadata = fields.pop("metadata")
        defaults = {
            **fields,
            **seal.as_model_fields(),
            "base_period": self.base_period,
            "base_index_value": self.base_level,
            "computation_metadata": metadata,
            "computed_at": timezone.now(),
            "published_at": timezone.now() if fields.get("is_published") else None,
        }
        value, created = IndexValue.objects.update_or_create(
            methodology_code=self.methodology_code,
            index_date=index_date,
            lead_window_days=lead_window_days,
            route=route,
            defaults=defaults,
        )
        self._attach_growth_rates(value)
        AuditLog.record(
            AuditAction.INDEX_COMPILED if created else AuditAction.INDEX_PUBLISHED,
            summary=f"{value.scope} T+{lead_window_days} {index_date} = {value.index_value}",
            entity=value,
            context={"provenance_hash": value.provenance_hash, "created": created},
        )
        return value

    def _attach_growth_rates(self, value: IndexValue) -> None:
        """Period-on-period and year-on-year, computed once at publication."""
        previous_day = self._level_on(value, value.index_date - timedelta(days=1))
        previous_year = self._level_on(value, value.index_date - timedelta(days=365))
        updates: dict[str, Any] = {}
        with localcontext(_CTX):
            if previous_day and previous_day > _ZERO:
                updates["period_on_period_pct"] = (
                    (value.index_value / previous_day - Decimal(1)) * _HUNDRED
                ).quantize(Decimal("0.000001"))
            if previous_year and previous_year > _ZERO:
                updates["year_on_year_pct"] = (
                    (value.index_value / previous_year - Decimal(1)) * _HUNDRED
                ).quantize(Decimal("0.000001"))
        if updates:
            IndexValue.objects.filter(pk=value.pk).update(**updates)
            for key, item in updates.items():
                setattr(value, key, item)

    @staticmethod
    def _level_on(value: IndexValue, when: date) -> Decimal | None:
        row = IndexValue.objects.filter(
            methodology_code=value.methodology_code,
            route=value.route,
            lead_window_days=value.lead_window_days,
            index_date=when,
            is_published=True,
        ).values_list("index_value", flat=True).first()
        return Decimal(row) if row is not None else None

    def _previous_hash(self, *, route: Route | None, lead_window_days: int | None, before: date) -> str:
        row = (
            IndexValue.objects.filter(
                methodology_code=self.methodology_code,
                lead_window_days=lead_window_days,
                index_date__lt=before,
            )
            .filter(Q(route=route) if route is not None else Q(route__isnull=True))
            .order_by("-index_date")
            .values_list("provenance_hash", flat=True)
            .first()
        )
        return row or ""

    @staticmethod
    def _seal_body(seal: Any) -> dict[str, Any]:
        """JSON-safe copy of the exact body that was hashed, for verification."""
        body = dict(seal.payload)
        body["index_value"] = str(body["index_value"])
        body["index_date"] = body["index_date"].isoformat()
        body["computed_at"] = body["computed_at"].isoformat()
        diagnostics = body.get("diagnostics") or {}
        body["diagnostics"] = {
            key: (str(item) if isinstance(item, Decimal) else item)
            for key, item in diagnostics.items()
        }
        return body


def compile_index_for_date(
    index_date: date, lead_windows: Iterable[int] | None = None
) -> list[CompilationOutcome]:
    """Compile every configured lead window for one day."""
    windows = list(lead_windows or settings.APIX["LEAD_WINDOWS"])
    compiler = IndexCompiler()
    return [compiler.compile(index_date, window) for window in windows]
