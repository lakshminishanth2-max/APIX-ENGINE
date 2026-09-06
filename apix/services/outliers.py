"""Robust outlier screening: Modified Z-Score over Median Absolute Deviation.

Why not a standard Z-score
--------------------------
Fare samples inside a stratum are small (often 10-40 quotes), right-skewed and
routinely contaminated.  The mean and standard deviation have a breakdown point
of 1/n: a single mis-parsed business-class fare drags both, inflates sigma, and
*hides the very point that caused the damage*.  The median and the MAD have a
50% breakdown point - half the sample can be garbage before the estimator is
compromised.

The statistic (Iglewicz & Hoaglin, 1993)
----------------------------------------
.. math::

    M_i = \\frac{0.6745\\,(x_i - \\tilde{x})}{\\mathrm{MAD}},
    \\qquad \\mathrm{MAD} = \\mathrm{median}(|x_i - \\tilde{x}|)

``0.6745`` is :math:`\\Phi^{-1}(0.75)`, which rescales the MAD into a consistent
estimator of :math:`\\sigma` under normality, so the familiar ``|M| > 3.5``
threshold means roughly "beyond 3.5 robust standard deviations".

Degenerate case
---------------
If more than half the sample is identical, ``MAD = 0`` and :math:`M_i` is
undefined.  The detector then falls back to the mean-absolute-deviation form
:math:`M_i = (x_i - \\tilde{x}) / (1.253314\\,\\mathrm{MeanAD})` - where
:math:`1.253314 = \\sqrt{\\pi/2}` - and reports which scale it used, so the
choice is auditable rather than hidden.

Log space
---------
Airfares are multiplicative: a fare at 2x the median and one at 0.5x are
equally distant in economic terms, but not in rupees.  Screening therefore runs
on ``ln(price)`` by default, which makes the test symmetric and stops the
detector from flagging every premium cabin in a mixed sample.

Flag, do not delete
-------------------
A surge is often a real market event - a festival, a fare-cap breach, a
competitor's capacity cut.  Deleting it would smooth away exactly the signal RBI
cares about.  Records are therefore **flagged** (``is_outlier = True``,
``modified_z_score`` persisted), excluded from the *elementary aggregate*, and
kept in full for diagnostics and for the published outlier count.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Final, TypeVar

__all__ = [
    "MADOutlierDetector",
    "OutlierConfig",
    "OutlierDirection",
    "OutlierMethod",
    "OutlierReport",
    "OutlierVerdict",
    "mad",
    "mean_absolute_deviation",
    "median",
    "modified_z_scores",
]

T = TypeVar("T")

_ZERO: Final[Decimal] = Decimal("0")
_TWO: Final[Decimal] = Decimal("2")

#: Phi^-1(0.75); rescales MAD to a consistent estimator of sigma.
MAD_CONSISTENCY_CONSTANT: Final[Decimal] = Decimal("0.6745")
#: sqrt(pi/2); the equivalent constant for the mean absolute deviation.
MEANAD_CONSISTENCY_CONSTANT: Final[Decimal] = Decimal("1.253314")
DEFAULT_THRESHOLD: Final[Decimal] = Decimal("3.5")

_CTX: Final[Context] = Context(prec=34, rounding=ROUND_HALF_EVEN)
_SCORE_QUANT: Final[Decimal] = Decimal("0.000001")


class OutlierMethod(StrEnum):
    MODIFIED_Z_MAD = "MODIFIED_Z_MAD"
    MODIFIED_Z_MEANAD = "MODIFIED_Z_MEANAD"
    DEGENERATE_ZERO_SCALE = "DEGENERATE_ZERO_SCALE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class OutlierDirection(StrEnum):
    NONE = "NONE"
    HIGH = "HIGH"
    LOW = "LOW"


# --------------------------------------------------------------------------- #
# Robust statistics - exact Decimal throughout
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
    """Median absolute deviation about the median."""
    if not values:
        raise ValueError("MAD of an empty sample is undefined")
    centre = center if center is not None else median(values)
    return median([abs(value - centre) for value in values])


def mean_absolute_deviation(values: Sequence[Decimal], center: Decimal | None = None) -> Decimal:
    if not values:
        raise ValueError("mean absolute deviation of an empty sample is undefined")
    centre = center if center is not None else median(values)
    with localcontext(_CTX):
        return sum((abs(value - centre) for value in values), _ZERO) / Decimal(len(values))


def modified_z_scores(
    values: Sequence[Decimal],
) -> tuple[tuple[Decimal, ...], Decimal, Decimal, OutlierMethod]:
    """Return ``(scores, centre, scale, method)``.

    ``scale`` is the denominator actually applied, so every score in a published
    report can be recomputed from the report alone.
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
                # Every observation identical: no dispersion, hence no outliers.
                return tuple(_ZERO for _ in values), centre, _ZERO, OutlierMethod.DEGENERATE_ZERO_SCALE

        scores = tuple(((value - centre) / scale).quantize(_SCORE_QUANT) for value in values)
        return scores, centre, scale, method


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OutlierVerdict:
    index: int
    value: Decimal
    score: Decimal
    is_outlier: bool
    direction: OutlierDirection = OutlierDirection.NONE
    label: str | None = None


@dataclass(frozen=True, slots=True)
class OutlierReport:
    """Auditable record of one screening pass over one homogeneous sample."""

    method: OutlierMethod
    threshold: Decimal
    sample_size: int
    center: Decimal
    scale: Decimal
    log_space: bool
    verdicts: tuple[OutlierVerdict, ...] = ()
    note: str | None = None

    @property
    def outliers(self) -> tuple[OutlierVerdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.is_outlier)

    @property
    def outlier_count(self) -> int:
        return len(self.outliers)

    @property
    def inlier_values(self) -> tuple[Decimal, ...]:
        return tuple(v.value for v in self.verdicts if not v.is_outlier)

    def as_dict(self) -> dict[str, object]:
        return {
            "method": str(self.method),
            "threshold": str(self.threshold),
            "sample_size": self.sample_size,
            "median": str(self.center),
            "scale": str(self.scale),
            "log_space": self.log_space,
            "outliers": self.outlier_count,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class OutlierConfig:
    threshold: Decimal = DEFAULT_THRESHOLD
    #: Below this, robust statistics are meaningless - nothing is flagged.
    min_samples: int = 5
    log_space: bool = True
    #: Never flag more than this share of a stratum, however extreme the scores.
    #: A stratum where a third of the fares "look wrong" is a stratification
    #: problem, not thirty outliers.
    max_contamination: Decimal = Decimal("0.25")


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
class MADOutlierDetector:
    """Modified Z-Score screen, applied strictly within a homogeneous stratum.

    Always screen inside a comparable cell - route x carrier x cabin x lead
    window.  Run across strata it would simply rediscover that business class
    costs more than economy.
    """

    def __init__(self, config: OutlierConfig | None = None) -> None:
        self.config = config or OutlierConfig()

    def detect(
        self, values: Sequence[Decimal], *, labels: Sequence[str | None] | None = None
    ) -> OutlierReport:
        sample = tuple(values)
        tags = tuple(labels or (None,) * len(sample))
        if len(tags) != len(sample):
            raise ValueError("labels must be the same length as values")

        if len(sample) < self.config.min_samples:
            return OutlierReport(
                method=OutlierMethod.INSUFFICIENT_DATA,
                threshold=self.config.threshold,
                sample_size=len(sample),
                center=median(sample) if sample else _ZERO,
                scale=_ZERO,
                log_space=False,
                verdicts=tuple(
                    OutlierVerdict(i, value, _ZERO, False, OutlierDirection.NONE, tags[i])
                    for i, value in enumerate(sample)
                ),
                note=f"sample of {len(sample)} below min_samples={self.config.min_samples}",
            )

        working, log_space, note = self._working_space(sample)
        scores, centre, scale, method = modified_z_scores(working)
        return OutlierReport(
            method=method,
            threshold=self.config.threshold,
            sample_size=len(sample),
            center=centre,
            scale=scale,
            log_space=log_space,
            verdicts=self._apply_threshold(sample, tags, scores),
            note=note,
        )

    def detect_grouped(self, groups: Mapping[str, Sequence[Decimal]]) -> dict[str, OutlierReport]:
        return {key: self.detect(values) for key, values in groups.items()}

    def screen_records(
        self,
        records: Iterable[T],
        *,
        value_of: Callable[[T], Decimal | None],
        stratum_of: Callable[[T], str],
        label_of: Callable[[T], str | None] | None = None,
    ) -> tuple[dict[str, OutlierReport], dict[int, OutlierVerdict]]:
        """Screen arbitrary records, keyed by ``id()`` for write-back.

        Records whose value is ``None`` - sold out, unpriced - are excluded from
        the sample entirely.  They are not zero and must not influence the
        median.
        """
        buckets: dict[str, list[tuple[T, Decimal]]] = {}
        for record in records:
            value = value_of(record)
            if value is None or value <= _ZERO:
                continue
            buckets.setdefault(stratum_of(record), []).append((record, value))

        reports: dict[str, OutlierReport] = {}
        verdicts: dict[int, OutlierVerdict] = {}
        for stratum, members in buckets.items():
            report = self.detect(
                [value for _, value in members],
                labels=[label_of(record) for record, _ in members] if label_of else None,
            )
            reports[stratum] = report
            for verdict in report.verdicts:
                verdicts[id(members[verdict.index][0])] = verdict
        return reports, verdicts

    # -- internals ---------------------------------------------------------- #
    def _working_space(self, values: Sequence[Decimal]) -> tuple[tuple[Decimal, ...], bool, str | None]:
        if not self.config.log_space:
            return tuple(values), False, None
        if any(value <= _ZERO for value in values):
            return tuple(values), False, "log space skipped: sample contains non-positive values"
        try:
            with localcontext(_CTX):
                return tuple(value.ln() for value in values), True, None
        except InvalidOperation:  # pragma: no cover - guarded above
            return tuple(values), False, "log space skipped: logarithm failed"

    def _apply_threshold(
        self,
        sample: Sequence[Decimal],
        labels: Sequence[str | None],
        scores: Sequence[Decimal],
    ) -> tuple[OutlierVerdict, ...]:
        threshold = self.config.threshold
        candidates = sorted(
            (i for i, score in enumerate(scores) if abs(score) > threshold),
            key=lambda i: abs(scores[i]),
            reverse=True,
        )
        cap = int(Decimal(len(sample)) * self.config.max_contamination)
        flagged = set(candidates[: max(cap, 1)]) if candidates else set()

        return tuple(
            OutlierVerdict(
                index=i,
                value=value,
                score=scores[i],
                is_outlier=i in flagged,
                direction=(
                    OutlierDirection.HIGH if i in flagged and scores[i] > _ZERO
                    else OutlierDirection.LOW if i in flagged
                    else OutlierDirection.NONE
                ),
                label=labels[i],
            )
            for i, value in enumerate(sample)
        )
