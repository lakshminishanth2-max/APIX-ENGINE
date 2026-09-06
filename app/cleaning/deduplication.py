"""Cross-source deduplication by SHA-256 identity and content digests.

The same physical seat is quoted by the airline, two OTAs and a metasearch
aggregator.  Counting it four times would silently reweight the index towards
whichever route the most aggregators happen to cover, so observations are
collapsed to one survivor per *(carrier, flight, departure, cabin, currency,
observation window)* identity.

Two digests are computed for every record:

``identity_digest``
    SHA-256 over the business key only.  Records sharing it describe the same
    seat and form one :class:`DuplicateGroup`.

``content_digest``
    SHA-256 over the business key **plus** every monetary field.  Equal content
    digests mean an exact re-report (safe to drop silently); differing content
    digests inside one identity group mean the sources *disagree on price*,
    which is recorded as a conflict with its spread so it can be alerted on.

``None`` and ``Decimal("0")`` hash to different tokens, so the "Missing != Zero"
rule from :mod:`app.cleaning.normalizer` survives into the digest layer.

Survivor selection is deterministic: source trust, then completeness, then
freshness, then the lowest content digest as a final stable tie-break.  The same
input batch always yields the same survivor, in any order, on any worker.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.cleaning.normalizer import ComponentState, NormalizedFare
from app.collectors.base import SourceType

__all__ = [
    "CrossSourceDeduplicator",
    "DedupConfig",
    "DedupResult",
    "DedupStats",
    "DuplicateGroup",
    "ResolutionReason",
    "TrustPolicy",
    "content_digest",
    "identity_digest",
]

_UTC: Final = timezone.utc
_ZERO: Final[Decimal] = Decimal("0")

#: Distinct sentinels so an unknown component can never hash like a zero one.
_MISSING_TOKEN: Final[str] = "\x00MISSING"
_NULL_TOKEN: Final[str] = "\x00NULL"
_FIELD_SEP: Final[str] = "\x1f"

#: Lower rank wins.  Direct carrier data outranks resold OTA inventory.
DEFAULT_TRUST_RANK: Final[Mapping[SourceType, int]] = MappingProxyType(
    {
        SourceType.REGULATORY: 0,
        SourceType.AIRLINE_DIRECT: 1,
        SourceType.GDS: 2,
        SourceType.OTA: 3,
        SourceType.METASEARCH: 4,
        SourceType.MOCK: 9,
    }
)


class ResolutionReason(StrEnum):
    UNIQUE = "unique"
    EXACT_DUPLICATE = "exact_duplicate"
    SOURCE_TRUST = "source_trust"
    COMPLETENESS = "completeness"
    FRESHNESS = "freshness"
    DIGEST_TIE_BREAK = "digest_tie_break"


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #
def _token(value: object | None) -> str:
    if value is None:
        return _MISSING_TOKEN
    if isinstance(value, Decimal):
        # Normalise scale so 100 and 100.00 hash identically.
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.astimezone(_UTC).isoformat()
    text = str(value).strip()
    return text.upper() if text else _NULL_TOKEN


def _digest(parts: Sequence[object | None]) -> str:
    return hashlib.sha256(_FIELD_SEP.join(_token(p) for p in parts).encode("utf-8")).hexdigest()


def _floor_ts(moment: datetime, window: timedelta) -> datetime:
    seconds = int(window.total_seconds())
    epoch = int(moment.astimezone(_UTC).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=_UTC)


def _bucket_time(departure_time: str | None, minutes: int) -> str | None:
    """Round a HH:MM departure into a tolerance bucket for fuzzy matching."""
    if not departure_time or minutes <= 0:
        return departure_time
    try:
        hour, minute = (int(part) for part in departure_time.split(":")[:2])
    except ValueError:
        return departure_time
    total = (hour * 60 + minute) // minutes * minutes
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def identity_digest(fare: NormalizedFare, config: DedupConfig | None = None) -> str:
    """SHA-256 of the business key: which seat, in which observation window."""
    cfg = config or DedupConfig()
    window = _floor_ts(fare.observation_ts, timedelta(minutes=cfg.observation_window_minutes))
    flight_key = (
        fare.flight_number
        if fare.flight_number and cfg.match_on_flight_number
        else _bucket_time(fare.departure_time, cfg.departure_time_tolerance_minutes)
    )
    return _digest(
        (
            fare.origin,
            fare.destination,
            fare.departure_date.isoformat(),
            fare.carrier_code,
            flight_key,
            str(fare.cabin),
            fare.currency,
            window.isoformat(),
            fare.days_to_departure,
        )
    )


def content_digest(fare: NormalizedFare, config: DedupConfig | None = None) -> str:
    """SHA-256 of the business key plus every monetary value."""
    return _digest(
        (
            identity_digest(fare, config),
            fare.total_fare,
            fare.base_fare,
            fare.surcharges,
            fare.taxes,
            fare.fees,
        )
    )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class TrustPolicy(BaseModel):
    """Ranking used to pick a survivor; lower rank wins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_source_type: Mapping[SourceType, int] = Field(default_factory=lambda: dict(DEFAULT_TRUST_RANK))
    by_source_id: Mapping[str, int] = Field(default_factory=dict)
    default_rank: int = 50

    def rank(self, *, source_id: str, source_type: SourceType | None) -> int:
        if source_id in self.by_source_id:
            return self.by_source_id[source_id]
        if source_type is not None and source_type in self.by_source_type:
            return self.by_source_type[source_type]
        return self.default_rank


class DedupConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Observations within the same window are candidates for collapsing.
    observation_window_minutes: int = Field(default=60, ge=1, le=1_440)
    #: Fall back to a departure-time bucket when a flight number is absent.
    departure_time_tolerance_minutes: int = Field(default=15, ge=0, le=180)
    match_on_flight_number: bool = True
    #: Relative price gap above which a group is flagged as a real conflict.
    conflict_threshold: Decimal = Field(default=Decimal("0.02"), ge=_ZERO, le=Decimal("1"))
    trust: TrustPolicy = TrustPolicy()


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Candidate:
    fare: NormalizedFare
    content: str
    trust_rank: int
    completeness: int
    observed_at: datetime

    def sort_key(self) -> tuple[int, int, float, str]:
        # trust asc, completeness desc, freshness desc, digest asc
        return (self.trust_rank, -self.completeness, -self.observed_at.timestamp(), self.content)


class DuplicateGroup(BaseModel):
    """One physical seat, as reported by one or more sources."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    winner: NormalizedFare
    winner_content_digest: str
    reason: ResolutionReason
    member_count: int = Field(ge=1)
    source_ids: tuple[str, ...]
    discarded: tuple[NormalizedFare, ...] = ()
    distinct_content_digests: int = Field(ge=1)
    price_min: Decimal
    price_max: Decimal
    conflict: bool = False

    @property
    def price_spread(self) -> Decimal:
        return self.price_max - self.price_min

    @property
    def relative_spread(self) -> Decimal:
        return (self.price_spread / self.price_min) if self.price_min > _ZERO else _ZERO


class DedupStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_count: int = Field(ge=0)
    survivor_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    group_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    exact_duplicate_count: int = Field(ge=0)

    @property
    def duplicate_ratio(self) -> float:
        return (self.duplicate_count / self.input_count) if self.input_count else 0.0


@dataclass(frozen=True, slots=True)
class DedupResult:
    survivors: tuple[NormalizedFare, ...]
    groups: tuple[DuplicateGroup, ...]
    stats: DedupStats

    def conflicts(self) -> tuple[DuplicateGroup, ...]:
        return tuple(g for g in self.groups if g.conflict)


# --------------------------------------------------------------------------- #
# Deduplicator
# --------------------------------------------------------------------------- #
class CrossSourceDeduplicator:
    """Streaming, deterministic cross-source deduplicator.

    Use :meth:`deduplicate` for a batch, or :meth:`add` / :meth:`finalize` to
    fold results in as collectors report, without buffering the whole run.
    """

    def __init__(
        self,
        config: DedupConfig | None = None,
        *,
        source_types: Mapping[str, SourceType] | None = None,
    ) -> None:
        self._config = config or DedupConfig()
        self._source_types = dict(source_types or {})
        self._buckets: defaultdict[str, list[_Candidate]] = defaultdict(list)
        self._input_count = 0

    @property
    def config(self) -> DedupConfig:
        return self._config

    def register_source(self, source_id: str, source_type: SourceType) -> None:
        """Teach the trust policy which family a ``source_id`` belongs to."""
        self._source_types[source_id] = source_type

    # -- streaming API ------------------------------------------------------ #
    def add(self, fare: NormalizedFare) -> str:
        """Fold one record in; returns its identity digest."""
        identity = identity_digest(fare, self._config)
        source_type = self._source_types.get(fare.source_id)
        self._buckets[identity].append(
            _Candidate(
                fare=fare,
                content=content_digest(fare, self._config),
                trust_rank=self._config.trust.rank(source_id=fare.source_id, source_type=source_type),
                completeness=self._completeness(fare),
                observed_at=fare.observation_ts,
            )
        )
        self._input_count += 1
        return identity

    def extend(self, fares: Iterable[NormalizedFare]) -> None:
        for fare in fares:
            self.add(fare)

    def finalize(self) -> DedupResult:
        """Resolve every bucket and reset the deduplicator for reuse."""
        groups: list[DuplicateGroup] = []
        survivors: list[NormalizedFare] = []
        duplicates = 0
        conflicts = 0
        exact = 0

        for identity in sorted(self._buckets):
            group = self._resolve(identity, self._buckets[identity])
            groups.append(group)
            survivors.append(group.winner)
            duplicates += group.member_count - 1
            conflicts += int(group.conflict)
            if group.reason is ResolutionReason.EXACT_DUPLICATE:
                exact += group.member_count - 1

        stats = DedupStats(
            input_count=self._input_count,
            survivor_count=len(survivors),
            duplicate_count=duplicates,
            group_count=len(groups),
            conflict_count=conflicts,
            exact_duplicate_count=exact,
        )
        self.reset()
        return DedupResult(tuple(survivors), tuple(groups), stats)

    def reset(self) -> None:
        self._buckets = defaultdict(list)
        self._input_count = 0

    # -- batch API ---------------------------------------------------------- #
    def deduplicate(self, fares: Iterable[NormalizedFare]) -> DedupResult:
        self.reset()
        self.extend(fares)
        return self.finalize()

    # -- internals ---------------------------------------------------------- #
    def _resolve(self, identity: str, candidates: list[_Candidate]) -> DuplicateGroup:
        ordered = sorted(candidates, key=_Candidate.sort_key)
        winner, *losers = ordered
        prices = [c.fare.total_fare for c in ordered]
        price_min, price_max = min(prices), max(prices)
        distinct = {c.content for c in ordered}

        spread_ratio = ((price_max - price_min) / price_min) if price_min > _ZERO else _ZERO
        conflict = len(distinct) > 1 and spread_ratio > self._config.conflict_threshold

        return DuplicateGroup(
            identity=identity,
            winner=winner.fare,
            winner_content_digest=winner.content,
            reason=self._reason(ordered),
            member_count=len(ordered),
            source_ids=tuple(sorted({c.fare.source_id for c in ordered})),
            discarded=tuple(c.fare for c in losers),
            distinct_content_digests=len(distinct),
            price_min=price_min,
            price_max=price_max,
            conflict=conflict,
        )

    @staticmethod
    def _reason(ordered: list[_Candidate]) -> ResolutionReason:
        if len(ordered) == 1:
            return ResolutionReason.UNIQUE
        if len({c.content for c in ordered}) == 1:
            return ResolutionReason.EXACT_DUPLICATE
        first, second = ordered[0], ordered[1]
        if first.trust_rank != second.trust_rank:
            return ResolutionReason.SOURCE_TRUST
        if first.completeness != second.completeness:
            return ResolutionReason.COMPLETENESS
        if first.observed_at != second.observed_at:
            return ResolutionReason.FRESHNESS
        return ResolutionReason.DIGEST_TIE_BREAK

    @staticmethod
    def _completeness(fare: NormalizedFare) -> int:
        """How much usable structure a record carries; higher survives."""
        score = sum(2 for audit in fare.audit.components if audit.state is ComponentState.PRESENT)
        score += sum(1 for audit in fare.audit.components if audit.state is ComponentState.EXPLICIT_ZERO)
        score += 3 if fare.audit.breakdown_trusted else 0
        score += 2 if fare.flight_number else 0
        score -= 2 * len(fare.issues)
        return score
