"""APIx index calculation: Jevons elementary indices, DGCA-weighted Laspeyres.

Two stages, deliberately separated because they answer different questions.

**Elementary (within a stratum) - Jevons.**  A stratum is a homogeneous cell
(route x carrier x cabin x days-to-departure band).  Inside it we have matched
price pairs and no reliable quantities, which is exactly the case the Jevons
index is designed for: the geometric mean of price relatives.  It is computed in
log space -

    I = 100 * exp( (1/n) * SUM ln(p_t,i / p_0,i) )

- not as a product of relatives.  With hundreds of pairs a naive product either
overflows or bleeds precision; the log form is numerically stable, makes
symmetric trimming of extreme relatives trivial, and is the standard CPI
treatment.  Every logarithm and exponential here is exact ``Decimal``.

**Aggregate (across strata) - Laspeyres with DGCA weights.**  Passenger volumes
come from DGCA traffic statistics, which are published with a lag, so the
weights are fixed to a base reference period: that is precisely a Laspeyres
form.  Strata with no usable data are dropped and the remaining weights are
renormalised, with the resulting ``weight_coverage`` published alongside the
level - an index built on 60% of the weight base must say so.

Every published level is sealed into a SHA-256 lineage record
(:mod:`app.index_engine.provenance`) that binds the inputs, the weights and the
exact methodology version used.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.index_engine.provenance import (
    LineageBuilder,
    LineageGraph,
    NodeKind,
    ProvenanceRecord,
    sha256_hex,
)

__all__ = [
    "DGCAWeight",
    "DGCAWeightSet",
    "ElementaryResult",
    "IndexCalculator",
    "IndexResult",
    "JevonsCalculator",
    "LaspeyresAggregator",
    "MatchedPair",
    "MethodologyRegistry",
    "MethodologyVersion",
    "StratumIndex",
    "StratumObservations",
    "chain_link",
]

logger = logging.getLogger(__name__)

_UTC: Final = timezone.utc
_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_HUNDRED: Final[Decimal] = Decimal("100")

#: 34 significant digits: comfortably beyond IEEE-754 double, so log-space
#: aggregation of thousands of relatives never loses a basis point.
_CTX: Final[Context] = Context(prec=34, rounding=ROUND_HALF_EVEN)
_LEVEL_QUANT: Final[Decimal] = Decimal("0.0001")
_WEIGHT_QUANT: Final[Decimal] = Decimal("0.00000001")


# --------------------------------------------------------------------------- #
# Methodology versioning
# --------------------------------------------------------------------------- #
class ElementaryFormula(StrEnum):
    JEVONS = "jevons"
    DUTOT = "dutot"
    CARLI = "carli"


class AggregationFormula(StrEnum):
    LASPEYRES = "laspeyres"
    PAASCHE = "paasche"


class MethodologyVersion(BaseModel):
    """Immutable, content-addressed description of how an index is computed.

    The digest covers every parameter that can move a printed level, so two
    runs agreeing on the digest are guaranteed to have applied the same rules.
    Publishing a changed parameter requires a new version - records already in
    the ledger keep pointing at the methodology they were computed under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^APIX-IDX-\d+\.\d+\.\d+$")
    effective_from: date
    base_period: str
    base_level: Decimal = Field(default=_HUNDRED, gt=_ZERO)
    elementary_formula: ElementaryFormula = ElementaryFormula.JEVONS
    aggregation_formula: AggregationFormula = AggregationFormula.LASPEYRES
    weight_scheme: str = "DGCA-PAX-SHARE"
    #: Symmetric trimming of extreme log-relatives inside a stratum.
    trim_fraction: Decimal = Field(default=Decimal("0.02"), ge=_ZERO, lt=Decimal("0.5"))
    min_pairs_per_stratum: int = Field(default=5, ge=1, le=10_000)
    #: Refuse to publish a headline below this share of the DGCA weight base.
    min_weight_coverage: Decimal = Field(default=Decimal("0.60"), gt=_ZERO, le=_ONE)
    outlier_threshold: Decimal = Field(default=Decimal("3.5"), gt=_ZERO)
    decimal_precision: int = Field(default=34, ge=16, le=60)
    notes: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        """SHA-256 over every rule-bearing parameter (excluding notes)."""
        return sha256_hex(
            {
                "version": self.version,
                "effective_from": self.effective_from,
                "base_period": self.base_period,
                "base_level": self.base_level,
                "elementary_formula": self.elementary_formula,
                "aggregation_formula": self.aggregation_formula,
                "weight_scheme": self.weight_scheme,
                "trim_fraction": self.trim_fraction,
                "min_pairs_per_stratum": self.min_pairs_per_stratum,
                "min_weight_coverage": self.min_weight_coverage,
                "outlier_threshold": self.outlier_threshold,
                "decimal_precision": self.decimal_precision,
            }
        )


DEFAULT_METHODOLOGY: Final[MethodologyVersion] = MethodologyVersion(
    version="APIX-IDX-1.0.0",
    effective_from=date(2025, 4, 1),
    base_period="2025-04",
    notes="Launch methodology: trimmed Jevons elementary, DGCA-weighted Laspeyres aggregate.",
)


class MethodologyRegistry:
    """Effective-dated catalogue of methodology versions."""

    def __init__(self, versions: Iterable[MethodologyVersion] = (DEFAULT_METHODOLOGY,)) -> None:
        self._versions: dict[str, MethodologyVersion] = {}
        for version in versions:
            self.register(version)

    def register(self, version: MethodologyVersion) -> MethodologyVersion:
        existing = self._versions.get(version.version)
        if existing is not None and existing.digest != version.digest:
            raise ValueError(
                f"methodology {version.version} already registered with a different digest; "
                "publish a new version instead of mutating an existing one"
            )
        self._versions[version.version] = version
        return version

    def get(self, version: str) -> MethodologyVersion:
        try:
            return self._versions[version]
        except KeyError:
            raise KeyError(f"unknown methodology version {version!r}") from None

    def resolve(self, as_of: date) -> MethodologyVersion:
        """Latest methodology in force on ``as_of``."""
        candidates = [v for v in self._versions.values() if v.effective_from <= as_of]
        if not candidates:
            raise LookupError(f"no methodology effective on {as_of.isoformat()}")
        return max(candidates, key=lambda v: (v.effective_from, v.version))

    def all(self) -> tuple[MethodologyVersion, ...]:
        return tuple(sorted(self._versions.values(), key=lambda v: (v.effective_from, v.version)))


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
class MatchedPair(BaseModel):
    """One matched-model observation: the same seat product in two periods."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    base_price: Decimal = Field(gt=_ZERO)
    current_price: Decimal = Field(gt=_ZERO)

    @property
    def relative(self) -> Decimal:
        with localcontext(_CTX):
            return self.current_price / self.base_price


class StratumObservations(BaseModel):
    """All matched pairs for one homogeneous cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stratum_id: str
    pairs: tuple[MatchedPair, ...] = ()
    quotes_observed: int = Field(default=0, ge=0)

    @property
    def digest(self) -> str:
        return sha256_hex(
            {
                "stratum_id": self.stratum_id,
                "pairs": [
                    {"item_id": p.item_id, "base": p.base_price, "current": p.current_price} for p in self.pairs
                ],
            }
        )


class DGCAWeight(BaseModel):
    """Passenger volume for one stratum, from DGCA traffic statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stratum_id: str
    passengers: Decimal = Field(ge=_ZERO)


class DGCAWeightSet(BaseModel):
    """Fixed base-period weight base; the Laspeyres ``q_0``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_period: str
    source_document: str
    published_on: date
    weights: tuple[DGCAWeight, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique(self) -> Self:
        ids = [w.stratum_id for w in self.weights]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate stratum_id in DGCA weight set")
        if sum((w.passengers for w in self.weights), _ZERO) <= _ZERO:
            raise ValueError("DGCA weight set has zero total passengers")
        return self

    @property
    def total_passengers(self) -> Decimal:
        return sum((w.passengers for w in self.weights), _ZERO)

    def shares(self) -> dict[str, Decimal]:
        """Normalised passenger shares over the full weight base."""
        total = self.total_passengers
        with localcontext(_CTX):
            return {w.stratum_id: (w.passengers / total).quantize(_WEIGHT_QUANT) for w in self.weights}

    def share_of(self, stratum_id: str) -> Decimal:
        return self.shares().get(stratum_id, _ZERO)

    def staleness_days(self, as_of: date) -> int:
        return (as_of - self.published_on).days

    @property
    def digest(self) -> str:
        return sha256_hex(
            {
                "reference_period": self.reference_period,
                "source_document": self.source_document,
                "published_on": self.published_on,
                "weights": [{"stratum_id": w.stratum_id, "passengers": w.passengers} for w in self.weights],
            }
        )


# --------------------------------------------------------------------------- #
# Elementary index: Jevons in log space
# --------------------------------------------------------------------------- #
class ElementaryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index_level: Decimal
    #: Mean log-relative actually used (after trimming).
    mean_log_relative: Decimal
    pairs_used: int = Field(ge=0)
    pairs_trimmed: int = Field(default=0, ge=0)
    pairs_rejected: int = Field(default=0, ge=0)
    formula: ElementaryFormula = ElementaryFormula.JEVONS


class JevonsCalculator:
    """Geometric mean of price relatives, evaluated in log space."""

    def __init__(self, methodology: MethodologyVersion = DEFAULT_METHODOLOGY) -> None:
        self._methodology = methodology

    @property
    def methodology(self) -> MethodologyVersion:
        return self._methodology

    def elementary_index(self, pairs: Sequence[MatchedPair]) -> ElementaryResult:
        """Jevons index (base = ``methodology.base_level``) for one stratum."""
        usable = [p for p in pairs if p.base_price > _ZERO and p.current_price > _ZERO]
        rejected = len(pairs) - len(usable)

        if not usable:
            return ElementaryResult(
                index_level=_ZERO,
                mean_log_relative=_ZERO,
                pairs_used=0,
                pairs_rejected=rejected,
                formula=self._methodology.elementary_formula,
            )

        with localcontext(_CTX):
            # ln(p_t / p_0) = ln(p_t) - ln(p_0): one division, two logs, no
            # product of relatives that could overflow or lose precision.
            logs = sorted((p.current_price / p.base_price).ln() for p in usable)
            kept, trimmed = self._trim(logs)
            mean_log = sum(kept, _ZERO) / Decimal(len(kept))
            level = (self._methodology.base_level * mean_log.exp()).quantize(_LEVEL_QUANT)

        return ElementaryResult(
            index_level=level,
            mean_log_relative=mean_log.quantize(Decimal("0.00000001")),
            pairs_used=len(kept),
            pairs_trimmed=trimmed,
            pairs_rejected=rejected,
            formula=ElementaryFormula.JEVONS,
        )

    def geometric_mean(self, values: Sequence[Decimal]) -> Decimal:
        """Geometric mean of positive values, computed as exp(mean(ln(v)))."""
        positive = [v for v in values if v > _ZERO]
        if not positive:
            raise ValueError("geometric mean requires at least one positive value")
        with localcontext(_CTX):
            return (sum((v.ln() for v in positive), _ZERO) / Decimal(len(positive))).exp()

    def _trim(self, ordered_logs: list[Decimal]) -> tuple[list[Decimal], int]:
        """Symmetric trim of the most extreme log-relatives."""
        fraction = self._methodology.trim_fraction
        if fraction <= _ZERO or len(ordered_logs) < self._methodology.min_pairs_per_stratum:
            return ordered_logs, 0
        cut = int(Decimal(len(ordered_logs)) * fraction)
        if cut == 0 or len(ordered_logs) - 2 * cut < 1:
            return ordered_logs, 0
        return ordered_logs[cut : len(ordered_logs) - cut], 2 * cut


# --------------------------------------------------------------------------- #
# Aggregation: DGCA-weighted Laspeyres
# --------------------------------------------------------------------------- #
class StratumIndex(BaseModel):
    """Published elementary index for one stratum, with its weight."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stratum_id: str
    index_level: Decimal
    pairs_used: int = Field(ge=0)
    pairs_trimmed: int = Field(default=0, ge=0)
    pairs_rejected: int = Field(default=0, ge=0)
    raw_weight_share: Decimal = Field(ge=_ZERO)
    effective_weight: Decimal = Field(default=_ZERO, ge=_ZERO)
    contribution: Decimal = Decimal("0")
    included: bool = True
    exclusion_reason: str | None = None
    input_digest: str | None = None


class LaspeyresAggregator:
    """Fixed-base-weight aggregation across strata."""

    def __init__(self, methodology: MethodologyVersion = DEFAULT_METHODOLOGY) -> None:
        self._methodology = methodology

    def aggregate(
        self,
        elementary: Mapping[str, ElementaryResult],
        weights: DGCAWeightSet,
    ) -> tuple[Decimal, Decimal, tuple[StratumIndex, ...]]:
        """Return ``(headline_level, weight_coverage, per-stratum detail)``.

        Strata without a usable elementary index are excluded and the surviving
        DGCA shares are renormalised, so the headline is a true weighted mean of
        what was actually observed rather than a silently zero-filled one.
        """
        shares = weights.shares()
        rows: list[StratumIndex] = []
        included_weight = _ZERO

        for stratum_id, share in sorted(shares.items()):
            result = elementary.get(stratum_id)
            if result is None:
                rows.append(
                    StratumIndex(
                        stratum_id=stratum_id,
                        index_level=_ZERO,
                        pairs_used=0,
                        raw_weight_share=share,
                        included=False,
                        exclusion_reason="no observations for stratum",
                    )
                )
                continue
            if result.pairs_used < self._methodology.min_pairs_per_stratum or result.index_level <= _ZERO:
                rows.append(
                    StratumIndex(
                        stratum_id=stratum_id,
                        index_level=result.index_level,
                        pairs_used=result.pairs_used,
                        pairs_trimmed=result.pairs_trimmed,
                        pairs_rejected=result.pairs_rejected,
                        raw_weight_share=share,
                        included=False,
                        exclusion_reason=(
                            f"only {result.pairs_used} matched pairs "
                            f"(minimum {self._methodology.min_pairs_per_stratum})"
                        ),
                    )
                )
                continue

            included_weight += share
            rows.append(
                StratumIndex(
                    stratum_id=stratum_id,
                    index_level=result.index_level,
                    pairs_used=result.pairs_used,
                    pairs_trimmed=result.pairs_trimmed,
                    pairs_rejected=result.pairs_rejected,
                    raw_weight_share=share,
                    included=True,
                )
            )

        if included_weight <= _ZERO:
            return _ZERO, _ZERO, tuple(rows)

        with localcontext(_CTX):
            finalized: list[StratumIndex] = []
            headline = _ZERO
            for row in rows:
                if not row.included:
                    finalized.append(row)
                    continue
                effective = (row.raw_weight_share / included_weight).quantize(_WEIGHT_QUANT)
                contribution = (effective * row.index_level).quantize(_LEVEL_QUANT)
                headline += effective * row.index_level
                finalized.append(
                    row.model_copy(update={"effective_weight": effective, "contribution": contribution})
                )
            coverage = included_weight.quantize(_WEIGHT_QUANT)
            return headline.quantize(_LEVEL_QUANT), coverage, tuple(finalized)

    def laspeyres_from_prices(
        self,
        *,
        base_prices: Mapping[str, Decimal],
        current_prices: Mapping[str, Decimal],
        base_quantities: Mapping[str, Decimal],
    ) -> Decimal:
        """Direct Laspeyres form: ``100 * SUM(p_t q_0) / SUM(p_0 q_0)``.

        Used for the published cross-check against the two-stage aggregation.
        """
        common = sorted(set(base_prices) & set(current_prices) & set(base_quantities))
        if not common:
            raise ValueError("no overlapping items between prices and base quantities")
        with localcontext(_CTX):
            numerator = sum((current_prices[k] * base_quantities[k] for k in common), _ZERO)
            denominator = sum((base_prices[k] * base_quantities[k] for k in common), _ZERO)
            if denominator <= _ZERO:
                raise ValueError("base-period expenditure is zero; Laspeyres undefined")
            return (self._methodology.base_level * numerator / denominator).quantize(_LEVEL_QUANT)


def chain_link(previous_level: Decimal, current_relative: Decimal) -> Decimal:
    """Chain a period-on-period relative onto a published level."""
    if previous_level <= _ZERO or current_relative <= _ZERO:
        raise ValueError("chain linking requires positive level and relative")
    with localcontext(_CTX):
        return (previous_level * current_relative).quantize(_LEVEL_QUANT)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IndexResult:
    """A published index level plus everything needed to defend it."""

    index_id: str
    period: str
    level: Decimal
    methodology: MethodologyVersion
    strata: tuple[StratumIndex, ...]
    weight_coverage: Decimal
    weight_set_digest: str
    pairs_used: int
    pairs_rejected: int
    publishable: bool
    suppression_reason: str | None
    provenance: ProvenanceRecord
    lineage: LineageGraph
    computed_at: datetime

    @property
    def included_strata(self) -> tuple[StratumIndex, ...]:
        return tuple(s for s in self.strata if s.included)

    def summary(self) -> dict[str, object]:
        return {
            "index_id": self.index_id,
            "period": self.period,
            "level": format(self.level, "f"),
            "methodology_version": self.methodology.version,
            "methodology_digest": self.methodology.digest,
            "weight_coverage": format(self.weight_coverage, "f"),
            "strata_included": len(self.included_strata),
            "strata_total": len(self.strata),
            "pairs_used": self.pairs_used,
            "publishable": self.publishable,
            "suppression_reason": self.suppression_reason,
            "record_digest": self.provenance.record_digest,
            "lineage_root": self.provenance.lineage_root,
        }


class IndexCalculator:
    """Composes the elementary and aggregate stages and seals the lineage."""

    def __init__(
        self,
        methodology: MethodologyVersion = DEFAULT_METHODOLOGY,
        *,
        registry: MethodologyRegistry | None = None,
    ) -> None:
        self._methodology = methodology
        self._registry = registry or MethodologyRegistry((methodology,))
        self._jevons = JevonsCalculator(methodology)
        self._laspeyres = LaspeyresAggregator(methodology)

    @property
    def methodology(self) -> MethodologyVersion:
        return self._methodology

    @classmethod
    def for_date(cls, as_of: date, registry: MethodologyRegistry) -> IndexCalculator:
        """Build a calculator pinned to the methodology in force on a date."""
        return cls(registry.resolve(as_of), registry=registry)

    def compute(
        self,
        *,
        index_id: str,
        period: str,
        observations: Sequence[StratumObservations],
        weights: DGCAWeightSet,
        previous_record_digest: str | None = None,
        computed_at: datetime | None = None,
    ) -> IndexResult:
        """Compute one published level with a full, verifiable lineage."""
        moment = computed_at or datetime.now(_UTC)
        builder = LineageBuilder()

        methodology_node = builder.add_source(
            payload=self._methodology.digest,
            label=self._methodology.version,
            kind=NodeKind.METHODOLOGY,
            metadata={"version": self._methodology.version, "base_period": self._methodology.base_period},
        )
        weights_node = builder.add_source(
            payload=weights.digest,
            label=f"dgca:{weights.reference_period}",
            kind=NodeKind.WEIGHT_SET,
            metadata={
                "reference_period": weights.reference_period,
                "source_document": weights.source_document,
            },
        )

        elementary: dict[str, ElementaryResult] = {}
        stratum_nodes: dict[str, str] = {}
        input_digests: list[str] = []
        pairs_used = 0
        pairs_rejected = 0

        for stratum in sorted(observations, key=lambda s: s.stratum_id):
            source_node = builder.add_source(
                payload=stratum.digest,
                label=stratum.stratum_id,
                kind=NodeKind.OUTLIER_SCREENED,
                metadata={"pairs": str(len(stratum.pairs)), "quotes_observed": str(stratum.quotes_observed)},
            )
            input_digests.append(source_node.digest)

            result = self._jevons.elementary_index(stratum.pairs)
            elementary[stratum.stratum_id] = result
            pairs_used += result.pairs_used
            pairs_rejected += result.pairs_rejected

            node = builder.add_transform(
                operation="jevons_elementary_index",
                parents=(source_node, methodology_node),
                payload={
                    "stratum_id": stratum.stratum_id,
                    "index_level": result.index_level,
                    "mean_log_relative": result.mean_log_relative,
                    "pairs_used": result.pairs_used,
                },
                params={
                    "formula": ElementaryFormula.JEVONS,
                    "trim_fraction": self._methodology.trim_fraction,
                    "min_pairs": self._methodology.min_pairs_per_stratum,
                    "base_level": self._methodology.base_level,
                },
                kind=NodeKind.ELEMENTARY_INDEX,
                label=stratum.stratum_id,
            )
            stratum_nodes[stratum.stratum_id] = node.digest

        headline, coverage, strata = self._laspeyres.aggregate(elementary, weights)
        strata = tuple(
            row.model_copy(update={"input_digest": stratum_nodes.get(row.stratum_id)}) for row in strata
        )

        publishable, suppression = self._publishability(headline, coverage)

        builder.add_transform(
            operation="dgca_weighted_laspeyres_aggregate",
            parents=(*sorted(stratum_nodes.values()), weights_node.digest, methodology_node.digest),
            payload={
                "index_id": index_id,
                "period": period,
                "level": headline,
                "weight_coverage": coverage,
                "strata": [
                    {
                        "stratum_id": row.stratum_id,
                        "level": row.index_level,
                        "effective_weight": row.effective_weight,
                        "included": row.included,
                    }
                    for row in strata
                ],
            },
            params={
                "formula": AggregationFormula.LASPEYRES,
                "weight_scheme": self._methodology.weight_scheme,
                "weight_reference_period": weights.reference_period,
                "min_weight_coverage": self._methodology.min_weight_coverage,
            },
            kind=NodeKind.AGGREGATE_INDEX,
            label=f"{index_id}:{period}",
        )

        graph = builder.build()
        record = ProvenanceRecord.seal(
            index_id=index_id,
            period=period,
            index_level=headline,
            methodology_version=self._methodology.version,
            methodology_digest=self._methodology.digest,
            graph=graph,
            input_digests=input_digests,
            weight_set_digest=weights.digest,
            previous_record_digest=previous_record_digest,
            metadata={
                "weight_coverage": format(coverage, "f"),
                "publishable": str(publishable).lower(),
                "strata_included": str(sum(1 for s in strata if s.included)),
            },
            created_at=moment,
        )

        if not publishable:
            logger.warning(
                "index suppressed index_id=%s period=%s reason=%s coverage=%s",
                index_id,
                period,
                suppression,
                coverage,
            )

        return IndexResult(
            index_id=index_id,
            period=period,
            level=headline,
            methodology=self._methodology,
            strata=strata,
            weight_coverage=coverage,
            weight_set_digest=weights.digest,
            pairs_used=pairs_used,
            pairs_rejected=pairs_rejected,
            publishable=publishable,
            suppression_reason=suppression,
            provenance=record,
            lineage=graph,
            computed_at=moment,
        )

    def _publishability(self, level: Decimal, coverage: Decimal) -> tuple[bool, str | None]:
        if level <= _ZERO:
            return False, "no stratum met the minimum matched-pair requirement"
        if coverage < self._methodology.min_weight_coverage:
            return False, (
                f"weight coverage {coverage} below methodology minimum "
                f"{self._methodology.min_weight_coverage}"
            )
        return True, None
