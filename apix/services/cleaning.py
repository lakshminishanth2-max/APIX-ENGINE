"""The 7-stage cleaning, validation and statistical-hygiene pipeline.

Updated for robust ingestion of messy live carrier and OTA payloads.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from collections.abc import Iterable, Sequence
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apix.collectors.registry import SourceRegistry, UnknownSourceError
from apix.enums import CabinClass, FareFlag, InventoryStatus, QualityGrade
from apix.models import CanonicalFare, CollectionRun, RawObservation, Route, Source
from apix.services.currency import ParseStatus, parse_money
from apix.services.outliers import MADOutlierDetector, OutlierConfig, OutlierDirection

__all__ = ["CleaningReport", "FareCleaningPipeline", "StageCounters", "clean_raw_observations"]

logger = logging.getLogger(__name__)

IST: Final = ZoneInfo("Asia/Kolkata")
UTC: Final = ZoneInfo("UTC")

_ZERO: Final[Decimal] = Decimal("0")
_CENT: Final[Decimal] = Decimal("0.01")
_MISSING_TOKEN: Final[str] = "\x00MISSING"
_FIELD_SEP: Final[str] = "\x1f"

COMPONENT_FIELDS: Final[tuple[str, ...]] = ("base_fare", "udf", "psf", "asf", "gst", "other_charges")
_ABS_TOLERANCE: Final[Decimal] = Decimal("0.05")
_REL_TOLERANCE: Final[Decimal] = Decimal("0.005")

_CABIN_SYNONYMS: Final[dict[str, str]] = {
    "y": CabinClass.ECONOMY, "e": CabinClass.ECONOMY, "eco": CabinClass.ECONOMY,
    "econ": CabinClass.ECONOMY, "economy": CabinClass.ECONOMY, "coach": CabinClass.ECONOMY,
    "economy class": CabinClass.ECONOMY, "main cabin": CabinClass.ECONOMY,
    "standard": CabinClass.ECONOMY, "saver": CabinClass.ECONOMY, "flexi": CabinClass.ECONOMY,
    "w": CabinClass.PREMIUM_ECONOMY, "pe": CabinClass.PREMIUM_ECONOMY,
    "premium": CabinClass.PREMIUM_ECONOMY, "premium economy": CabinClass.PREMIUM_ECONOMY,
    "comfort": CabinClass.PREMIUM_ECONOMY, "comfort plus": CabinClass.PREMIUM_ECONOMY,
    "c": CabinClass.BUSINESS, "j": CabinClass.BUSINESS, "biz": CabinClass.BUSINESS,
    "business": CabinClass.BUSINESS, "business class": CabinClass.BUSINESS,
    "f": CabinClass.FIRST, "first": CabinClass.FIRST, "first class": CabinClass.FIRST,
}

# Regex to strip and isolate airline designators and numeric flight codes
_CARRIER_CLEAN_RE = re.compile(r"[^A-Z0-9]")
_FLIGHT_NUM_RE = re.compile(r"(\d+)")


@dataclass
class StageCounters:
    extracted: int = 0
    currency_failures: int = 0
    canonicalisation_failures: int = 0
    structural_rejects: int = 0
    partial_breakdown: int = 0
    total_mismatch: int = 0
    sold_out: int = 0
    price_unavailable: int = 0
    duplicates: int = 0
    cross_source_conflicts: int = 0
    outliers: int = 0
    written: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "extracted": self.extracted,
            "currency_failures": self.currency_failures,
            "canonicalisation_failures": self.canonicalisation_failures,
            "structural_rejects": self.structural_rejects,
            "partial_breakdown": self.partial_breakdown,
            "total_mismatch": self.total_mismatch,
            "sold_out": self.sold_out,
            "price_unavailable": self.price_unavailable,
            "duplicates": self.duplicates,
            "cross_source_conflicts": self.cross_source_conflicts,
            "outliers": self.outliers,
            "written": self.written,
        }


@dataclass
class CleaningReport:
    counters: StageCounters = field(default_factory=StageCounters)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    outlier_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_processed: int = 0
    raw_failed: int = 0

    def reject(self, stage: str, reason: str, context: dict[str, Any] | None = None) -> None:
        self.rejections.append({"stage": stage, "reason": reason, **(context or {})})

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_processed": self.raw_processed,
            "raw_failed": self.raw_failed,
            "stages": self.counters.as_dict(),
            "rejections": self.rejections[:200],
            "rejection_total": len(self.rejections),
            "outlier_reports": self.outlier_reports,
        }


@dataclass
class StagedFare:
    raw_observation_id: int
    source: Source
    route: Route
    lead_window_days: int
    observation_date: date
    observed_at: datetime

    carrier_iata: str = ""
    flight_number: str = ""
    cabin: str = CabinClass.ECONOMY
    currency: str = "INR"
    departure_utc: datetime | None = None
    arrival_utc: datetime | None = None
    departure_local: datetime | None = None

    base_fare: Decimal | None = None
    udf: Decimal | None = None
    psf: Decimal | None = None
    asf: Decimal | None = None
    gst: Decimal | None = None
    other_charges: Decimal | None = None
    taxes_fees: Decimal | None = None
    total_fare: Decimal | None = None

    inventory_status: str = InventoryStatus.AVAILABLE
    seats_remaining: int | None = None
    is_refundable: bool | None = None
    fare_basis: str = ""
    booking_class: str = ""

    quality_grade: str = QualityGrade.B_TOTAL_ONLY
    flags: list[str] = field(default_factory=list)
    is_outlier: bool = False
    modified_z_score: Decimal | None = None
    outlier_stratum: str = ""
    fingerprint: str = ""
    content_hash: str = ""
    is_duplicate: bool = False
    dedup_reason: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def flag(self, value: str) -> None:
        if value not in self.flags:
            self.flags.append(value)

    @property
    def known_components(self) -> dict[str, Decimal]:
        return {
            name: getattr(self, name)
            for name in COMPONENT_FIELDS
            if getattr(self, name) is not None
        }

    @property
    def stratum_key(self) -> str:
        return f"{self.route.code}|{self.cabin}|T+{self.lead_window_days}|{self.observation_date.isoformat()}"


class FareCleaningPipeline:
    def __init__(self, *, detector: MADOutlierDetector | None = None) -> None:
        cfg = settings.APIX
        self.detector = detector or MADOutlierDetector(
            OutlierConfig(
                threshold=Decimal(str(cfg["OUTLIER_THRESHOLD"])),
                min_samples=int(cfg["OUTLIER_MIN_SAMPLE"]),
                log_space=True,
            )
        )
        self.report = CleaningReport()

    def run(
        self,
        raw_observations: Sequence[RawObservation],
        *,
        collection_run: CollectionRun | None = None,
    ) -> CleaningReport:
        staged: list[StagedFare] = []
        processed_raw_ids: list[int] = []

        for raw in raw_observations:
            try:
                staged.extend(self._process_raw(raw))
                self.report.raw_processed += 1
                processed_raw_ids.append(raw.pk)
            except Exception as exc:
                self.report.raw_failed += 1
                self.report.reject("extraction", str(exc), {"raw_id": raw.pk})
                logger.exception("raw observation failed", extra={"raw_id": raw.pk})
                RawObservation.objects.filter(pk=raw.pk).update(
                    is_processed=True, processed_at=timezone.now(), processing_error=str(exc)[:2_000]
                )

        staged = self._stage_6_deduplicate(staged)
        self._stage_7_screen_outliers(staged)
        self._persist(staged, collection_run=collection_run, processed_raw_ids=processed_raw_ids)
        return self.report

    def _process_raw(self, raw: RawObservation) -> list[StagedFare]:
        source: Source = raw.source
        try:
            collector_cls = SourceRegistry.get(source.code)
        except UnknownSourceError as exc:
            raise ValueError(f"no collector registered for source {source.code!r}") from exc

        collector = collector_cls(base_url=source.base_url, attributes=source.attributes)
        task = self._task_from_raw(raw)
        rows = collector.extract(raw.payload, task)
        self.report.counters.extracted += len(rows)

        staged: list[StagedFare] = []
        for row in rows:
            fare = StagedFare(
                raw_observation_id=raw.pk,
                source=source,
                route=raw.route,
                lead_window_days=raw.lead_window_days or 0,
                observation_date=raw.captured_at.astimezone(IST).date(),
                observed_at=raw.captured_at,
            )
            if not self._stage_2_currency(row, fare):
                continue
            if not self._stage_3_canonicalise(row, fare):
                continue
            if not self._stage_4_validate(fare):
                continue
            self._stage_5_audit_components(fare)
            fare.fingerprint = self._fingerprint(fare)
            fare.content_hash = self._content_hash(fare)
            staged.append(fare)
        return staged

    @staticmethod
    def _task_from_raw(raw: RawObservation) -> Any:
        from apix.collectors.base import CollectionTask
        route = raw.route
        return CollectionTask(
            route_code=route.code if route else "XXX-YYY",
            origin=route.origin if route else "XXX",
            destination=route.destination if route else "YYY",
            departure_date=raw.search_departure_date or raw.captured_at.date(),
            lead_window_days=raw.lead_window_days or 0,
            observation_date=raw.captured_at.astimezone(IST).date(),
        )

    def _stage_2_currency(self, row: dict[str, Any], fare: StagedFare) -> bool:
        currency_raw = str(row.get("currency") or settings.APIX["CURRENCY"]).upper().strip()
        expected = settings.APIX.get("CURRENCY", "INR")
        fare.currency = expected
        notes: dict[str, str] = {}

        if currency_raw != expected:
            fare.provenance["original_currency"] = currency_raw

        for name in (*COMPONENT_FIELDS, "taxes_fees", "total_fare"):
            if name not in row:
                continue
            result = parse_money(row[name], expected_currency=None)
            if result.status is ParseStatus.OK:
                setattr(fare, name, result.value)
            elif result.status is ParseStatus.MISSING:
                notes[name] = "missing_token"
            else:
                notes[name] = str(result.status)
                self.report.counters.currency_failures += 1
                if name == "total_fare":
                    self.report.reject(
                        "currency", f"total_fare unparseable: {result.note}",
                        {"raw_id": fare.raw_observation_id, "value": str(row[name])[:64]},
                    )
                    return False
        if notes:
            fare.provenance["currency_notes"] = notes
        return True

    def _stage_3_canonicalise(self, row: dict[str, Any], fare: StagedFare) -> bool:
        raw_carrier = str(row.get("carrier_iata") or "").strip().upper()
        # Clean carriers like "6E - INDIGO" or "AI/IX" down to base code
        clean_carrier = _CARRIER_CLEAN_RE.sub("", raw_carrier.split("/")[0].split("-")[0])[:3]
        if not (2 <= len(clean_carrier) <= 3):
            self.report.counters.canonicalisation_failures += 1
            self.report.reject("canonicalisation", "invalid carrier designator",
                               {"raw_id": fare.raw_observation_id, "value": raw_carrier})
            return False
        fare.carrier_iata = clean_carrier

        # Strip redundant leading carrier prefix first, then extract flight digits
        raw_number = str(row.get("flight_number") or "").strip().upper()
        if raw_number.startswith(clean_carrier):
            raw_number = raw_number[len(clean_carrier):].strip()

        digits = _FLIGHT_NUM_RE.findall(raw_number)
        if digits:
            # Store cleanly parsed flight digits (e.g., '615', '214')
            fare.flight_number = str(int(digits[0]))
        elif raw_number:
            fare.flight_number = raw_number
        else:
            fare.flight_number = ""

        fare.cabin = _CABIN_SYNONYMS.get(str(row.get("cabin") or "economy").strip().lower(), CabinClass.ECONOMY)
        fare.fare_basis = str(row.get("fare_basis") or "")[:32]
        fare.booking_class = str(row.get("booking_class") or "")[:4]

        seats = row.get("seats_remaining")
        if seats is not None:
            try:
                fare.seats_remaining = max(0, int(seats))
            except (TypeError, ValueError):
                fare.seats_remaining = None
        refundable = row.get("is_refundable")
        fare.is_refundable = bool(refundable) if refundable is not None else None

        # Resolve local IST departure time
        departure_local = self._parse_local(row.get("departure_local"))
        if departure_local is None:
            raw_status = str(row.get("inventory_status") or "").upper()
            is_unpriced = (
                raw_status in (InventoryStatus.SOLD_OUT, InventoryStatus.PRICE_UNAVAILABLE)
                or row.get("total_fare") in (None, "", "0", 0, Decimal("0"))
                or fare.total_fare is None
                or (fare.total_fare is not None and fare.total_fare <= _ZERO)
            )
            if is_unpriced:
                target_date = fare.observation_date + timedelta(days=fare.lead_window_days)
                departure_local = datetime.combine(target_date, datetime.min.time(), tzinfo=IST)
                fare.flag(FareFlag.MODELLED)
            else:
                self.report.counters.canonicalisation_failures += 1
                self.report.reject("canonicalisation", "undecodable departure time",
                                   {"raw_id": fare.raw_observation_id, "value": str(row.get("departure_local"))[:48]})
                return False
        fare.departure_local = departure_local
        fare.departure_utc = departure_local.astimezone(UTC)
        arrival_local = self._parse_local(row.get("arrival_local"))
        fare.arrival_utc = arrival_local.astimezone(UTC) if arrival_local else None

        # Evaluate pricing and enforce invariant: unpriced is NULL
        status = str(row.get("inventory_status") or InventoryStatus.AVAILABLE).upper()
        if status not in InventoryStatus.values:
            status = InventoryStatus.AVAILABLE
        is_zero_or_negative = fare.total_fare is not None and fare.total_fare <= _ZERO
        if status != InventoryStatus.AVAILABLE or fare.total_fare is None or is_zero_or_negative:
            if status == InventoryStatus.AVAILABLE and (fare.total_fare is None or is_zero_or_negative):
                status = InventoryStatus.PRICE_UNAVAILABLE
            fare.inventory_status = status
            for name in (*COMPONENT_FIELDS, "taxes_fees", "total_fare"):
                setattr(fare, name, None)
            fare.quality_grade = QualityGrade.D_UNUSABLE
            if status == InventoryStatus.SOLD_OUT:
                fare.flag(FareFlag.SOLD_OUT)
                self.report.counters.sold_out += 1
            else:
                fare.flag(FareFlag.PRICE_UNAVAILABLE)
                self.report.counters.price_unavailable += 1
        else:
            fare.inventory_status = InventoryStatus.AVAILABLE
        return True

    @staticmethod
    def _parse_local(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=IST)
        val_str = str(value).strip().replace("Z", "+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(val_str, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=IST)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(val_str)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
        except ValueError:
            return None

    def _stage_4_validate(self, fare: StagedFare) -> bool:
        if fare.route is None:
            self.report.counters.structural_rejects += 1
            self.report.reject("structural", "observation has no route", {"raw_id": fare.raw_observation_id})
            return False
        if fare.route.origin == fare.route.destination:
            self.report.counters.structural_rejects += 1
            self.report.reject("structural", "circular route", {"route": fare.route.code})
            return False

        for name in (*COMPONENT_FIELDS, "total_fare"):
            value = getattr(fare, name)
            if value is not None and value < _ZERO:
                self.report.counters.structural_rejects += 1
                self.report.reject("structural", f"negative {name}",
                                   {"raw_id": fare.raw_observation_id, "value": str(value)})
                return False

        if fare.departure_utc is not None:
            if fare.departure_utc < fare.observed_at - timedelta(hours=3):
                self.report.counters.structural_rejects += 1
                self.report.reject("structural", "departure precedes observation",
                                   {"raw_id": fare.raw_observation_id,
                                    "departure": fare.departure_utc.isoformat()})
                return False
            observed_lead = (fare.departure_utc.astimezone(IST).date() - fare.observation_date).days
            if abs(observed_lead - fare.lead_window_days) > 1:
                fare.flag(FareFlag.STALE_OBSERVATION)
                fare.provenance["lead_window_drift"] = observed_lead - fare.lead_window_days
        return True

    def _stage_5_audit_components(self, fare: StagedFare) -> None:
        if fare.total_fare is None:
            fare.quality_grade = QualityGrade.D_UNUSABLE
            return

        known = fare.known_components
        missing = [name for name in COMPONENT_FIELDS if getattr(fare, name) is None]

        for name, value in known.items():
            if value == _ZERO:
                fare.flag(FareFlag.EXPLICIT_ZERO)
                fare.provenance.setdefault("explicit_zeros", []).append(name)

        if len(missing) == 1 and len(known) == len(COMPONENT_FIELDS) - 1:
            residual = fare.total_fare - sum(known.values(), _ZERO)
            if residual >= _ZERO:
                setattr(fare, missing[0], residual.quantize(_CENT))
                fare.flag(FareFlag.COMPONENT_DERIVED)
                fare.provenance["derived_component"] = {missing[0]: str(residual)}
                missing = []
                known = fare.known_components

        if missing:
            fare.provenance["missing_components"] = missing
            currency_notes = fare.provenance.get("currency_notes", {})
            has_corrupt = any(
                name in missing and status != "missing_token"
                for name, status in currency_notes.items()
            )
            if has_corrupt:
                fare.flag(FareFlag.COMPONENT_UNPARSEABLE)
                fare.quality_grade = QualityGrade.C_SUSPECT
            else:
                fare.flag(FareFlag.PARTIAL_BREAKDOWN)
                fare.quality_grade = QualityGrade.B_TOTAL_ONLY
            self.report.counters.partial_breakdown += 1
            return

        component_sum = sum(known.values(), _ZERO)
        residual = fare.total_fare - component_sum
        tolerance = max(_ABS_TOLERANCE, abs(fare.total_fare) * _REL_TOLERANCE)
        fare.taxes_fees = (component_sum - (fare.base_fare or _ZERO)).quantize(_CENT)

        if abs(residual) <= tolerance:
            fare.quality_grade = QualityGrade.A_FULL_BREAKDOWN
        else:
            fare.flag(FareFlag.TOTAL_MISMATCH)
            fare.quality_grade = QualityGrade.C_SUSPECT
            fare.provenance["reconciliation"] = {
                "component_sum": str(component_sum),
                "total": str(fare.total_fare),
                "residual": str(residual),
            }
            self.report.counters.total_mismatch += 1

    def _stage_6_deduplicate(self, staged: list[StagedFare]) -> list[StagedFare]:
        clusters: dict[str, list[StagedFare]] = defaultdict(list)
        for fare in staged:
            clusters[fare.fingerprint].append(fare)

        for fingerprint, members in clusters.items():
            if len(members) == 1:
                members[0].dedup_reason = "unique"
                continue

            ordered = sorted(members, key=self._survivor_key)
            winner, losers = ordered[0], ordered[1:]
            winner.dedup_reason = self._dedup_reason(ordered)

            priced = [m.total_fare for m in ordered if m.total_fare is not None]
            if priced and len({m.content_hash for m in ordered}) > 1:
                spread = (max(priced) - min(priced)) / min(priced) if min(priced) > _ZERO else _ZERO
                if spread > Decimal("0.02"):
                    self.report.counters.cross_source_conflicts += 1
                    for member in ordered:
                        member.flag(FareFlag.CROSS_SOURCE_CONFLICT)
                    winner.provenance["cross_source_spread"] = str(spread.quantize(Decimal("0.0001")))

            for loser in losers:
                loser.is_duplicate = True
                loser.dedup_reason = "lost_tie_break"
                loser.flag(FareFlag.DUPLICATE_SUPPRESSED)
                loser.provenance["duplicate_of_fingerprint"] = fingerprint
                self.report.counters.duplicates += 1
        return staged

    @staticmethod
    def _survivor_key(fare: StagedFare) -> tuple[int, int, float, str]:
        completeness = len(fare.known_components) * 2
        completeness += 3 if fare.quality_grade == QualityGrade.A_FULL_BREAKDOWN else 0
        completeness += 2 if fare.flight_number else 0
        completeness += 2 if fare.total_fare is not None else 0
        return (
            fare.source.trust_rank,
            -completeness,
            -fare.observed_at.timestamp(),
            fare.content_hash,
        )

    @staticmethod
    def _dedup_reason(ordered: Sequence[StagedFare]) -> str:
        if len({member.content_hash for member in ordered}) == 1:
            return "exact_duplicate"
        first, second = ordered[0], ordered[1]
        if first.source.trust_rank != second.source.trust_rank:
            return "source_trust"
        if first.observed_at != second.observed_at:
            return "freshness"
        return "content_hash"

    def _stage_7_screen_outliers(self, staged: Sequence[StagedFare]) -> None:
        candidates = [fare for fare in staged if not fare.is_duplicate and fare.total_fare]
        reports, verdicts = self.detector.screen_records(
            candidates,
            value_of=lambda fare: fare.total_fare,
            stratum_of=lambda fare: fare.stratum_key,
            label_of=lambda fare: f"{fare.source.code}:{fare.flight_number}",
        )

        for fare in candidates:
            verdict = verdicts.get(id(fare))
            if verdict is None:
                continue
            fare.modified_z_score = verdict.score
            fare.outlier_stratum = fare.stratum_key
            if verdict.is_outlier:
                fare.is_outlier = True
                fare.flag(
                    FareFlag.OUTLIER_HIGH if verdict.direction is OutlierDirection.HIGH
                    else FareFlag.OUTLIER_LOW
                )
                if fare.quality_grade == QualityGrade.A_FULL_BREAKDOWN:
                    fare.quality_grade = QualityGrade.C_SUSPECT
                self.report.counters.outliers += 1

        self.report.outlier_reports = {key: report.as_dict() for key, report in reports.items()}

    @staticmethod
    def _token(value: Any) -> str:
        if value is None:
            return _MISSING_TOKEN
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        return str(value).strip().upper()

    @classmethod
    def _digest(cls, parts: Iterable[Any]) -> str:
        return hashlib.sha256(
            _FIELD_SEP.join(cls._token(part) for part in parts).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _fingerprint(cls, fare: StagedFare) -> str:
        return cls._digest((
            fare.route.code,
            fare.carrier_iata,
            fare.flight_number,
            fare.departure_utc,
            fare.cabin,
            fare.currency,
            fare.lead_window_days,
            fare.observation_date,
            fare.fare_basis,
            fare.booking_class,
        ))

    @classmethod
    def _content_hash(cls, fare: StagedFare) -> str:
        return cls._digest((
            fare.fingerprint,
            fare.total_fare, fare.base_fare, fare.udf, fare.psf,
            fare.asf, fare.gst, fare.other_charges,
            fare.inventory_status,
        ))

    @transaction.atomic
    def _persist(
        self,
        staged: Sequence[StagedFare],
        *,
        collection_run: CollectionRun | None,
        processed_raw_ids: Sequence[int] | None = None,
    ) -> None:
        if not staged:
            if processed_raw_ids:
                RawObservation.objects.filter(pk__in=processed_raw_ids).update(
                    is_processed=True, processed_at=timezone.now(), processing_error=""
                )
            return

        seen_conflict_keys: set[tuple[Any, str, Any]] = set()
        unique_staged: list[StagedFare] = []
        for fare in staged:
            key = (fare.source.code, fare.fingerprint, fare.observation_date)
            if key not in seen_conflict_keys:
                seen_conflict_keys.add(key)
                unique_staged.append(fare)

        rows = [
            CanonicalFare(
                raw_observation_id=fare.raw_observation_id,
                collection_run=collection_run,
                source=fare.source,
                route=fare.route,
                carrier_iata=fare.carrier_iata,
                flight_number=fare.flight_number,
                cabin=fare.cabin,
                departure_utc=fare.departure_utc,
                arrival_utc=fare.arrival_utc,
                departure_local=fare.departure_local,
                observed_at=fare.observed_at,
                observation_date=fare.observation_date,
                lead_window_days=fare.lead_window_days,
                currency=fare.currency,
                base_fare=fare.base_fare,
                udf=fare.udf,
                psf=fare.psf,
                asf=fare.asf,
                gst=fare.gst,
                other_charges=fare.other_charges,
                taxes_fees=fare.taxes_fees,
                total_fare=fare.total_fare,
                inventory_status=fare.inventory_status,
                seats_remaining=fare.seats_remaining,
                is_refundable=fare.is_refundable,
                fare_basis=fare.fare_basis,
                booking_class=fare.booking_class,
                quality_grade=fare.quality_grade,
                flags=fare.flags,
                is_outlier=fare.is_outlier,
                modified_z_score=fare.modified_z_score,
                outlier_stratum=fare.outlier_stratum,
                fingerprint=fare.fingerprint,
                content_hash=fare.content_hash,
                is_duplicate=fare.is_duplicate,
                dedup_reason=fare.dedup_reason,
                provenance=fare.provenance,
            )
            for fare in unique_staged
        ]

        CanonicalFare.objects.bulk_create(
            rows,
            batch_size=500,
            update_conflicts=True,
            unique_fields=["source", "fingerprint", "observation_date"],
            update_fields=[
                "total_fare", "base_fare", "udf", "psf", "asf", "gst", "other_charges",
                "taxes_fees", "inventory_status", "seats_remaining", "quality_grade",
                "flags", "is_outlier", "modified_z_score", "outlier_stratum",
                "content_hash", "is_duplicate", "dedup_reason", "provenance",
                "collection_run",
            ],
        )
        self.report.counters.written = len(rows)
        self._link_duplicates(staged)
        if processed_raw_ids:
            RawObservation.objects.filter(pk__in=processed_raw_ids).update(
                is_processed=True, processed_at=timezone.now(), processing_error=""
            )

    @staticmethod
    def _link_duplicates(staged: Sequence[StagedFare]) -> None:
        fingerprints = {fare.fingerprint for fare in staged if fare.is_duplicate}
        if not fingerprints:
            return
        winners = {
            row.fingerprint: row.pk
            for row in CanonicalFare.objects.filter(
                fingerprint__in=fingerprints, is_duplicate=False
            ).order_by("pk").only("pk", "fingerprint")
        }
        for fingerprint, winner_pk in winners.items():
            CanonicalFare.objects.filter(fingerprint=fingerprint, is_duplicate=True).update(
                duplicate_of_id=winner_pk
            )


def clean_raw_observations(
    raw_observations: Sequence[RawObservation], *, collection_run: CollectionRun | None = None
) -> CleaningReport:
    return FareCleaningPipeline().run(raw_observations, collection_run=collection_run)