"""Robust outlier detection via the Modified Z-Score (median / MAD).

Fare distributions are small, skewed and routinely contaminated: a single
mis-parsed business-class fare or a scraped "from" price will drag a mean and
standard deviation far enough to hide the very point that caused the damage.
The classic Z-score is therefore useless here.  Iglewicz & Hoaglin's Modified
Z-Score is not:

    M_i = 0.6745 * (x_i - median(x)) / MAD(x),   MAD = median(|x_i - median(x)|)

with ``|M_i| > 3.5`` flagged as an outlier.  The median and the MAD both have a
50% breakdown point, so up to half the sample can be garbage before the
detector itself is compromised.

Degenerate case: when more than half the sample is identical, ``MAD == 0`` and
the score is undefined.  The detector then falls back to the mean absolute
deviation form ``M_i = (x_i - median) / (1.253314 * MeanAD)``, and reports which
scale it used so the choice is auditable.

Everything is exact ``Decimal`` arithmetic - including the natural logs used by
log-space detection, which is the right space for multiplicative fare spreads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MADOutlierDetector",
    "OutlierConfig",
    "OutlierDirection",
    "OutlierMethod",
    "OutlierReport",
    "OutlierVerdict",
    "mad",
    "median",
    "modified_z_scores",
]

T = TypeVar("T")

_ZERO: Final[Decimal] = Decimal("0")
_TWO: Final[Decimal] = Decimal("2")

#: 0.6745 is the 0.75 quantile of the standard normal - it rescales the MAD
#: into a consistent estimator of sigma for normally distributed data.
MAD_CONSISTENCY_CONSTANT: Final[Decimal] = Decimal("0.6745")

#: 1.253314 = sqrt(pi/2); the equivalent constant for the mean abs deviation.
MEANAD_CONSISTENCY_CONSTANT: Final[Decimal] = Decimal("1.253314")

DEFAULT_THRESHOLD: Final[Decimal] = Decimal("3.5")

#: 34 digits keeps ratios and logs well clear of the 2-dp money scale.
_CTX: Final[Context] = Context(prec=34, rounding=ROUND_HALF_EVEN)

_SCORE_QUANT: Final[Decimal] = Decimal("0.0001")


class OutlierMethod(StrEnum):
    MODIFIED_Z_MAD = "modified_z_mad"
    MODIFIED_Z_MEANAD = "modified_z_meanad"
    DEGENERATE_ZERO_SCALE = "degenerate_zero_scale"
    INSUFFICIENT_DATA = "insufficient_data"


class OutlierDirection(StrEnum):
    NONE = "none"
    HIGH = "high"
    LOW = "low"


# --------------------------------------------------------------------------- #
# Robust statistics (exact Decimal)
# --------------------------------------------------------------------------- #
def median(values: Sequence[Decimal]) -> Decimal:
    """Exact median; averages the two central order statistics when even."""
    if not values:
        raise ValueError("median of an empty sample is undefined")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    with localcontext(_CTX):
        return (ordered[mid - 1] + ordered[mid]) / _TWO


def mad(values: Sequence[Decimal], center: Decimal | None = None) -> Decimal:
    """Median absolute deviation about the median (or a supplied centre)."""
    if not values:
        raise ValueError("MAD of an empty sample is undefined")
    mid = center if center is not None else median(values)
    return median([abs(v - mid) for v in values])


def mean_absolute_deviation(values: Sequence[Decimal], center: Decimal | None = None) -> Decimal:
    if not values:
        raise ValueError("mean absolute deviation of an empty sample is undefined")
    mid = center if center is not None else median(values)
    with localcontext(_CTX):
        return sum((abs(v - mid) for v in values), _ZERO) / Decimal(len(values))


def modified_z_scores(
    values: Sequence[Decimal],
) -> tuple[tuple[Decimal, ...], Decimal, Decimal, OutlierMethod]:
    """Return ``(scores, centre, scale, method)`` for a sample.

    ``scale`` is the denominator actually used, so a caller can reproduce every
    score from the report alone.
    """
    if not values:
        return (), _ZERO, _ZERO, OutlierMethod.INSUFFICIENT_DATA

    with localcontext(_CTX):
        centre = median(values)
        deviation = mad(values, centre)

        if deviation > _ZERO:
            scale = deviation / MAD_CONSISTENCY_CONSTANT
            method = OutlierMethod.MODIFIED_Z_MAD
        else:
            mean_dev = mean_absolute_deviation(values, centre)
            if mean_dev > _ZERO:
                scale = mean_dev * MEANAD_CONSISTENCY_CONSTANT
                method = OutlierMethod.MODIFIED_Z_MEANAD
            else:
                # Every observation is identical: no dispersion, no outliers.
                return tuple(_ZERO for _ in values), centre, _ZERO, OutlierMethod.DEGENERATE_ZERO_SCALE

        scores = tuple(((v - centre) / scale).quantize(_SCORE_QUANT) for v in values)
        return scores, centre, scale, method


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class OutlierVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    value: Decimal
    score: Decimal
    is_outlier: bool
    direction: OutlierDirection = OutlierDirection.NONE
    label: str | None = None


class OutlierReport(BaseModel):
    """Auditable record of one detection pass over one homogeneous sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: OutlierMethod
    threshold: Decimal
    sample_size: int = Field(ge=0)
    center: Decimal
    scale: Decimal
    log_space: bool = False
    verdicts: tuple[OutlierVerdict, ...] = ()
    note: str | None = None

    @property
    def outliers(self) -> tuple[OutlierVerdict, ...]:
        return tuple(v for v in self.verdicts if v.is_outlier)

    @property
    def inliers(self) -> tuple[OutlierVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.is_outlier)

    @property
    def outlier_count(self) -> int:
        return len(self.outliers)

    @property
    def contamination_rate(self) -> Decimal:
        if not self.sample_size:
            return _ZERO
        with localcontext(_CTX):
            return (Decimal(self.outlier_count) / Decimal(self.sample_size)).quantize(_SCORE_QUANT)

    def inlier_values(self) -> tuple[Decimal, ...]:
        return tuple(v.value for v in self.inliers)


class OutlierConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: Decimal = Field(default=DEFAULT_THRESHOLD, gt=_ZERO, le=Decimal("20"))
    #: Below this, robust statistics are meaningless - nothing is flagged.
    min_samples: int = Field(default=5, ge=3, le=1_000)
    #: Fares are multiplicative; detecting in log space treats a 2x and a 0.5x
    #: departure from the median symmetrically.
    log_space: bool = True
    #: Never flag more than this share of a sample, however extreme the scores.
    max_contamination: Decimal = Field(default=Decimal("0.25"), gt=_ZERO, le=Decimal("0.5"))


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Sample:
    values: tuple[Decimal, ...]
    labels: tuple[str | None, ...]


class MADOutlierDetector:
    """Modified Z-Score detector, applied per homogeneous stratum.

    Always run this *within* a comparable group (route x carrier x cabin x
    days-to-departure band).  Applied across strata it would simply flag the
    business-class fares.
    """

    def __init__(self, config: OutlierConfig | None = None) -> None:
        self._config = config or OutlierConfig()

    @property
    def config(self) -> OutlierConfig:
        return self._config

    def detect(
        self,
        values: Sequence[Decimal],
        *,
        labels: Sequence[str | None] | None = None,
    ) -> OutlierReport:
        """Score one sample and flag ``|M_i| > threshold``."""
        sample = _Sample(tuple(values), tuple(labels or (None,) * len(values)))
        if len(sample.values) != len(sample.labels):
            raise ValueError("labels must be the same length as values")

        if len(sample.values) < self._config.min_samples:
            return OutlierReport(
                method=OutlierMethod.INSUFFICIENT_DATA,
                threshold=self._config.threshold,
                sample_size=len(sample.values),
                center=median(sample.values) if sample.values else _ZERO,
                scale=_ZERO,
                log_space=False,
                verdicts=tuple(
                    OutlierVerdict(index=i, value=v, score=_ZERO, is_outlier=False, label=sample.labels[i])
                    for i, v in enumerate(sample.values)
                ),
                note=f"sample of {len(sample.values)} below min_samples={self._config.min_samples}",
            )

        working, log_space, note = self._to_working_space(sample.values)
        scores, center, scale, method = modified_z_scores(working)

        verdicts = self._apply_threshold(sample, scores)
        return OutlierReport(
            method=method,
            threshold=self._config.threshold,
            sample_size=len(sample.values),
            center=center,
            scale=scale,
            log_space=log_space,
            verdicts=verdicts,
            note=note,
        )

    def detect_grouped(
        self,
        groups: Mapping[str, Sequence[Decimal]],
    ) -> dict[str, OutlierReport]:
        """Run one independent pass per stratum key."""
        return {key: self.detect(values) for key, values in groups.items()}

    def detect_records(
        self,
        records: Iterable[T],
        *,
        value_of: Callable[[T], Decimal],
        stratum_of: Callable[[T], str],
        label_of: Callable[[T], str | None] | None = None,
    ) -> tuple[dict[str, OutlierReport], tuple[T, ...], tuple[T, ...]]:
        """Partition arbitrary records into ``(reports, inliers, outliers)``."""
        buckets: dict[str, list[T]] = {}
        for record in records:
            buckets.setdefault(stratum_of(record), []).append(record)

        reports: dict[str, OutlierReport] = {}
        inliers: list[T] = []
        outliers: list[T] = []
        for stratum, members in buckets.items():
            report = self.detect(
                [value_of(m) for m in members],
                labels=[label_of(m) for m in members] if label_of else None,
            )
            reports[stratum] = report
            flagged = {v.index for v in report.outliers}
            for position, member in enumerate(members):
                (outliers if position in flagged else inliers).append(member)
        return reports, tuple(inliers), tuple(outliers)

    def partition(self, values: Sequence[Decimal]) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
        report = self.detect(values)
        return report.inlier_values(), tuple(v.value for v in report.outliers)

    # -- internals ---------------------------------------------------------- #
    def _to_working_space(self, values: Sequence[Decimal]) -> tuple[tuple[Decimal, ...], bool, str | None]:
        if not self._config.log_space:
            return tuple(values), False, None
        if any(v <= _ZERO for v in values):
            return tuple(values), False, "log space skipped: sample contains non-positive values"
        try:
            with localcontext(_CTX):
                return tuple(v.ln() for v in values), True, None
        except InvalidOperation:  # pragma: no cover - guarded by the check above
            return tuple(values), False, "log space skipped: logarithm failed"

    def _apply_threshold(self, sample: _Sample, scores: Sequence[Decimal]) -> tuple[OutlierVerdict, ...]:
        threshold = self._config.threshold
        candidates = sorted(
            (i for i, s in enumerate(scores) if abs(s) > threshold),
            key=lambda i: abs(scores[i]),
            reverse=True,
        )
        cap = int(Decimal(len(sample.values)) * self._config.max_contamination)
        flagged = set(candidates[: max(cap, 1)]) if candidates else set()

        return tuple(
            OutlierVerdict(
                index=i,
                value=value,
                score=scores[i],
                is_outlier=i in flagged,
                direction=(
                    OutlierDirection.HIGH
                    if i in flagged and scores[i] > _ZERO
                    else OutlierDirection.LOW
                    if i in flagged
                    else OutlierDirection.NONE
                ),
                label=sample.labels[i],
            )
            for i, value in enumerate(sample.values)
        )
