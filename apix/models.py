"""Relational model for the Airfare Price Index.

Design rules that the schema itself enforces
--------------------------------------------
1. **Raw is immutable.**  :class:`RawObservation` rows are write-once.  Every
   published index value can be traced back to the exact bytes a source served,
   addressed by their SHA-256 ingress hash.
2. **Missing is not zero.**  Every monetary column on :class:`CanonicalFare` is
   nullable, and a ``CHECK`` constraint forbids a priced record with a NULL
   total or an unpriced record with a non-NULL total.  ``NULL`` means *unknown*
   or *no inventory*; ``0.00`` means *genuinely waived*.  Collapsing the two
   would corrupt both the base-fare sub-index and the Jevons geometric mean.
3. **Money is NUMERIC.**  Never ``double precision``.  Django ``DecimalField``
   maps to ``NUMERIC(p, s)`` and round-trips as :class:`decimal.Decimal`.
4. **Weights are versioned, never edited.**  :class:`RouteWeight` rows carry
   ``valid_from`` / ``valid_to``; a DGCA restatement adds a row, it never
   updates one, so a historical index remains reproducible.
5. **Published values are hash-chained.**  :class:`IndexValue` stores its own
   provenance hash plus its predecessor's, so a silently rewritten historical
   print breaks verification of every later value in the series.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar, Self

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q, QuerySet
from django.utils import timezone

from apix.enums import (
    AuditAction,
    CabinClass,
    CircuitState,
    CollectionStatus,
    ComplianceStatus,
    FareFlag,
    InventoryStatus,
    MethodologyCode,
    PayloadKind,
    QualityGrade,
    SourceKind,
)

__all__ = [
    "AuditLog",
    "CanonicalFare",
    "CollectionRun",
    "IndexValue",
    "RawObservation",
    "Route",
    "RouteWeight",
    "Source",
    "SourcePolicy",
]

IATA_VALIDATOR = RegexValidator(r"^[A-Z]{3}$", "Must be a 3-letter uppercase IATA code.")
CARRIER_VALIDATOR = RegexValidator(r"^[A-Z0-9]{2,3}$", "Must be a 2-3 character carrier designator.")
SHA256_VALIDATOR = RegexValidator(r"^[0-9a-f]{64}$", "Must be a lowercase hex SHA-256 digest.")


# --------------------------------------------------------------------------- #
# Source registry
# --------------------------------------------------------------------------- #
class SourceQuerySet(QuerySet["Source"]):
    def active(self) -> Self:
        return self.filter(is_active=True)

    def collectable(self) -> Self:
        """Active sources whose policy currently permits outbound traffic."""
        return self.active().filter(
            policy__compliance_status=ComplianceStatus.ACTIVE
        ).exclude(policy__circuit_state=CircuitState.TRIPPED_PERMANENT)

    def by_kind(self, kind: str) -> Self:
        return self.filter(kind=kind)


class Source(models.Model):
    """A carrier, OTA, GDS feed or synthetic generator we collect fares from.

    ``trust_rank`` orders survivors during cross-source deduplication: lower
    wins, so a carrier's own API (rank 10) beats a metasearch scrape (rank 40)
    when the two disagree about the same seat.
    """

    code = models.SlugField(
        max_length=64, unique=True,
        help_text="Stable machine key, e.g. 'indigo_api'. Matches the collector's registry id.",
    )
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=SourceKind.choices, db_index=True)
    carrier_iata = models.CharField(
        max_length=3, blank=True, default="", validators=[CARRIER_VALIDATOR],
        help_text="Set for carrier-owned sources; blank for OTAs and aggregators.",
    )
    base_url = models.URLField(max_length=500, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    trust_rank = models.PositiveSmallIntegerField(
        default=50, validators=[MinValueValidator(0), MaxValueValidator(999)],
        help_text="Deduplication precedence; lower wins.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    attributes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[SourceQuerySet] = SourceQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        db_table = "apix_source"
        ordering = ("trust_rank", "code")
        verbose_name = "source"
        verbose_name_plural = "sources"
        indexes = [models.Index(fields=("kind", "is_active"), name="ix_source_kind_active")]

    def __str__(self) -> str:
        return f"{self.code} ({self.get_kind_display()})"

    @property
    def is_synthetic(self) -> bool:
        return self.kind == SourceKind.MOCK_FEED


class SourcePolicy(models.Model):
    """Per-source compliance envelope: rate ceiling, robots, breaker state.

    The circuit breaker is *persisted* rather than kept in worker memory, so a
    halt survives worker restarts and is visible to every worker in the fleet
    plus the Django admin.  ``TRIPPED_PERMANENT`` is only ever cleared by an
    operator action, which writes an :class:`AuditLog` row.
    """

    source = models.OneToOneField(Source, on_delete=models.CASCADE, related_name="policy")

    # -- rate limiting ------------------------------------------------------ #
    requests_per_minute = models.PositiveIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(6_000)],
        help_text="Sustained ceiling enforced by the distributed Redis token bucket.",
    )
    burst = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1), MaxValueValidator(500)],
        help_text="Bucket capacity: how many requests may arrive back-to-back.",
    )
    max_concurrency = models.PositiveSmallIntegerField(default=1)

    # -- robots ------------------------------------------------------------- #
    robots_url = models.URLField(max_length=500, blank=True, default="")
    respect_robots = models.BooleanField(default=True)
    robots_last_checked = models.DateTimeField(null=True, blank=True)
    robots_last_verdict = models.CharField(max_length=64, blank=True, default="")
    crawl_delay_seconds = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True,
        help_text="Honoured Crawl-delay directive, if the host publishes one.",
    )
    user_agent = models.CharField(max_length=255, blank=True, default="")

    # -- breaker / compliance ---------------------------------------------- #
    compliance_status = models.CharField(
        max_length=32, choices=ComplianceStatus.choices,
        default=ComplianceStatus.ACTIVE, db_index=True,
    )
    circuit_state = models.CharField(
        max_length=20, choices=CircuitState.choices, default=CircuitState.CLOSED, db_index=True,
    )
    failure_threshold = models.PositiveSmallIntegerField(default=5)
    cooldown_seconds = models.PositiveIntegerField(default=900)
    consecutive_failures = models.PositiveIntegerField(default=0)
    opened_at = models.DateTimeField(null=True, blank=True)
    halted_at = models.DateTimeField(null=True, blank=True)
    halt_reason = models.TextField(blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    total_requests = models.BigIntegerField(default=0)
    total_failures = models.BigIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apix_source_policy"
        verbose_name = "source policy"
        verbose_name_plural = "source policies"
        constraints = [
            models.CheckConstraint(
                condition=Q(requests_per_minute__gte=1), name="ck_policy_rpm_positive",
            ),
            models.CheckConstraint(
                condition=Q(burst__gte=1), name="ck_policy_burst_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source.code}: {self.compliance_status}/{self.circuit_state}"

    @property
    def refill_rate_per_second(self) -> float:
        """Token-bucket refill rate derived from the per-minute ceiling."""
        return self.requests_per_minute / 60.0

    @property
    def is_collectable(self) -> bool:
        return (
            self.compliance_status == ComplianceStatus.ACTIVE
            and self.circuit_state != CircuitState.TRIPPED_PERMANENT
        )

    def halt(self, reason: str, *, status: str = ComplianceStatus.HALTED_BOT_CHALLENGE) -> None:
        """Latch the source off.  Deliberately irreversible without an operator."""
        self.compliance_status = status
        self.circuit_state = CircuitState.TRIPPED_PERMANENT
        self.halted_at = timezone.now()
        self.halt_reason = reason[:2_000]
        self.save(update_fields=[
            "compliance_status", "circuit_state", "halted_at", "halt_reason", "updated_at",
        ])


# --------------------------------------------------------------------------- #
# Reference geography and weights
# --------------------------------------------------------------------------- #
class RouteQuerySet(QuerySet["Route"]):
    def active(self) -> Self:
        return self.filter(is_active=True)

    def in_basket(self) -> Self:
        return self.active().filter(is_in_basket=True)


class Route(models.Model):
    """A domestic city-pair in the sampling frame.

    Routes are directional (DEL-BOM and BOM-DEL are distinct products with
    distinct yield curves) but the DGCA basket is defined on the directional
    codes listed in ``APIX["ROUTE_BASKET"]``.
    """

    code = models.CharField(
        max_length=7, unique=True, help_text="Canonical 'ORG-DST' code, e.g. DEL-BOM.",
    )
    origin = models.CharField(max_length=3, validators=[IATA_VALIDATOR], db_index=True)
    destination = models.CharField(max_length=3, validators=[IATA_VALIDATOR], db_index=True)
    origin_city = models.CharField(max_length=80, blank=True, default="")
    destination_city = models.CharField(max_length=80, blank=True, default="")
    distance_km = models.PositiveIntegerField(
        null=True, blank=True, help_text="Great-circle distance; used for per-km diagnostics.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    is_in_basket = models.BooleanField(
        default=False, db_index=True,
        help_text="Included in the published national composite index.",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[RouteQuerySet] = RouteQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        db_table = "apix_route"
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("origin", "destination"), name="uq_route_origin_destination"),
            models.CheckConstraint(
                condition=~Q(origin=models.F("destination")), name="ck_route_not_circular",
            ),
        ]
        indexes = [models.Index(fields=("is_in_basket", "is_active"), name="ix_route_basket_active")]

    def __str__(self) -> str:
        return self.code

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.origin = self.origin.upper()
        self.destination = self.destination.upper()
        self.code = f"{self.origin}-{self.destination}"
        super().save(*args, **kwargs)

    def clean(self) -> None:
        if self.origin.upper() == self.destination.upper():
            raise ValidationError({"destination": "A route cannot start and end at the same airport."})


class RouteWeightQuerySet(QuerySet["RouteWeight"]):
    def effective_on(self, on: date) -> Self:
        """Weight versions in force on a given date (half-open interval)."""
        return self.filter(valid_from__lte=on).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=on))


class RouteWeight(models.Model):
    """DGCA passenger-traffic share for one route over a validity interval.

    This is the ``q_0`` of the Laspeyres aggregation.  DGCA publishes traffic
    with a lag, which is precisely why a *fixed base-period* weight - Laspeyres
    - is the correct higher-level formula rather than a current-weighted one.
    """

    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="weights")
    valid_from = models.DateField(db_index=True)
    valid_to = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="Exclusive upper bound; NULL means 'current version'.",
    )
    passengers = models.BigIntegerField(
        validators=[MinValueValidator(0)],
        help_text="Annual/period passengers carried on the city-pair, per DGCA.",
    )
    share = models.DecimalField(
        max_digits=12, decimal_places=10,
        help_text="Normalised share of the basket's total passengers (0-1).",
    )
    reference_period = models.CharField(max_length=32, help_text="e.g. 'FY2024-25' or '2025-Q1'.")
    source_document = models.CharField(max_length=255, blank=True, default="")
    published_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[RouteWeightQuerySet] = RouteWeightQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        db_table = "apix_route_weight"
        ordering = ("route__code", "-valid_from")
        constraints = [
            models.UniqueConstraint(fields=("route", "valid_from"), name="uq_route_weight_version"),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=models.F("valid_from")),
                name="ck_route_weight_interval",
            ),
            models.CheckConstraint(
                condition=Q(share__gte=0) & Q(share__lte=1), name="ck_route_weight_share_unit",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.route.code} {self.reference_period}: {self.share}"


# --------------------------------------------------------------------------- #
# Collection runs
# --------------------------------------------------------------------------- #
class CollectionRun(models.Model):
    """One dispatched collection job; the unit the UI monitors."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(
        max_length=16, choices=CollectionStatus.choices,
        default=CollectionStatus.PENDING, db_index=True,
    )
    triggered_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="collection_runs",
    )
    trigger_reason = models.CharField(max_length=64, default="scheduled")
    celery_task_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    routes = models.ManyToManyField(Route, blank=True, related_name="collection_runs")
    lead_windows = ArrayField(models.PositiveSmallIntegerField(), default=list, blank=True)
    parameters = models.JSONField(default=dict, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    raw_count = models.IntegerField(default=0)
    canonical_count = models.IntegerField(default=0)
    duplicate_count = models.IntegerField(default=0)
    outlier_count = models.IntegerField(default=0)
    rejected_count = models.IntegerField(default=0)
    stats = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "apix_collection_run"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status", "-created_at"), name="ix_run_status_created")]

    def __str__(self) -> str:
        return f"run {self.id} [{self.status}]"

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def mark(self, status: str, *, error: str = "") -> None:
        self.status = status
        if status == CollectionStatus.RUNNING and self.started_at is None:
            self.started_at = timezone.now()
        if status in CollectionStatus.terminal():
            self.finished_at = timezone.now()
        if error:
            self.error = error[:4_000]
        self.save(update_fields=["status", "started_at", "finished_at", "error"])


# --------------------------------------------------------------------------- #
# Immutable ingress
# --------------------------------------------------------------------------- #
class RawObservationManager(models.Manager["RawObservation"]):
    def unprocessed(self) -> QuerySet[RawObservation]:
        return self.filter(is_processed=False).order_by("captured_at")


class RawObservation(models.Model):
    """Write-once capture of exactly what a source returned.

    Nothing in the platform may rewrite these rows: the SHA-256 ``ingress_hash``
    over the raw bytes is the anchor of the provenance chain, and an edited
    payload would invalidate every index value derived from it.  ``save()``
    refuses updates and ``delete()`` refuses outright; retention is handled by
    an explicit, audited archival command.
    """

    id = models.BigAutoField(primary_key=True)
    ingress_hash = models.CharField(
        max_length=64, unique=True, validators=[SHA256_VALIDATOR],
        help_text="SHA-256 over the raw response bytes - the lineage anchor.",
    )
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="raw_observations")
    collection_run = models.ForeignKey(
        CollectionRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="raw_observations",
    )
    route = models.ForeignKey(
        Route, null=True, blank=True, on_delete=models.PROTECT, related_name="raw_observations",
    )
    lead_window_days = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    search_departure_date = models.DateField(null=True, blank=True)

    payload_kind = models.CharField(max_length=8, choices=PayloadKind.choices, default=PayloadKind.JSON)
    payload = models.JSONField(
        help_text="Verbatim response. HTML/CSV bodies are wrapped as {'body': '<...>'}.",
    )
    request_url = models.URLField(max_length=1_000, blank=True, default="")
    request_method = models.CharField(max_length=8, default="GET")
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_headers = models.JSONField(default=dict, blank=True)
    content_bytes = models.PositiveIntegerField(default=0)

    captured_at = models.DateTimeField(db_index=True, help_text="UTC instant the payload was received.")
    collector = models.CharField(max_length=64, blank=True, default="")

    is_processed = models.BooleanField(default=False, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_error = models.TextField(blank=True, default="")

    objects: ClassVar[RawObservationManager] = RawObservationManager()

    class Meta:
        db_table = "apix_raw_observation"
        ordering = ("-captured_at",)
        verbose_name = "raw observation"
        indexes = [
            models.Index(fields=("source", "-captured_at"), name="ix_raw_source_captured"),
            models.Index(fields=("route", "lead_window_days", "-captured_at"), name="ix_raw_route_window"),
            models.Index(fields=("is_processed", "captured_at"), name="ix_raw_processing_queue"),
        ]

    def __str__(self) -> str:
        return f"raw {self.ingress_hash[:12]} from {self.source_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Allow inserts and processing-flag updates only."""
        if self.pk is not None:
            allowed = {"is_processed", "processed_at", "processing_error"}
            update_fields = set(kwargs.get("update_fields") or ())
            if not update_fields or not update_fields <= allowed:
                raise ValidationError(
                    "RawObservation is immutable; only the processing flags may be updated."
                )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        raise ValidationError(
            "RawObservation rows are immutable evidence and cannot be deleted. "
            "Use the audited `archive_raw_observations` management command."
        )


# --------------------------------------------------------------------------- #
# Canonical (cleaned) layer
# --------------------------------------------------------------------------- #
class CanonicalFareQuerySet(QuerySet["CanonicalFare"]):
    def priced(self) -> Self:
        return self.filter(total_fare__isnull=False, total_fare__gt=Decimal("0"))

    def survivors(self) -> Self:
        return self.filter(is_duplicate=False)

    def index_eligible(self) -> Self:
        """Records admissible to a published aggregate."""
        return (
            self.survivors()
            .priced()
            .filter(quality_grade__in=QualityGrade.index_eligible(), is_outlier=False, is_modeled=False)
        )

    def for_window(self, lead_window_days: int) -> Self:
        return self.filter(lead_window_days=lead_window_days)

    def observed_on(self, day: date) -> Self:
        return self.filter(observed_at__date=day)


class CanonicalFare(models.Model):
    """A cleaned, typed, deduplicated fare observation.

    Nullability is load-bearing.  ``total_fare IS NULL`` with
    ``inventory_status = SOLD_OUT`` records that a flight existed and had no
    purchasable seat - an economically meaningful scarcity signal that must not
    be read as a price of zero.  ``base_fare IS NULL`` with the
    ``PARTIAL_BREAKDOWN`` flag records that an OTA published only an all-in
    price, which keeps the base-fare sub-index honest instead of imputing zero.
    """

    id = models.BigAutoField(primary_key=True)

    raw_observation = models.ForeignKey(
        RawObservation, on_delete=models.PROTECT, related_name="canonical_fares",
    )
    collection_run = models.ForeignKey(
        CollectionRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="canonical_fares",
    )
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="canonical_fares")
    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="canonical_fares")

    # -- flight identity ---------------------------------------------------- #
    carrier_iata = models.CharField(max_length=3, validators=[CARRIER_VALIDATOR], db_index=True)
    flight_number = models.CharField(max_length=10, blank=True, default="")
    cabin = models.CharField(max_length=16, choices=CabinClass.choices, default=CabinClass.ECONOMY)
    departure_utc = models.DateTimeField(db_index=True)
    arrival_utc = models.DateTimeField(null=True, blank=True)
    departure_local = models.DateTimeField(
        null=True, blank=True, help_text="Departure in the origin airport's local time (IST).",
    )

    # -- sampling design ---------------------------------------------------- #
    observed_at = models.DateTimeField(db_index=True, help_text="UTC instant the price was seen.")
    observation_date = models.DateField(db_index=True, help_text="IST calendar date of observation.")
    lead_window_days = models.PositiveSmallIntegerField(
        db_index=True, help_text="Days between observation and departure: 1, 7, 15, 30 or 45.",
    )

    # -- money (all nullable: missing != zero) ------------------------------ #
    currency = models.CharField(max_length=3, default="INR")
    base_fare = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    udf = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="UDF",
        help_text="User Development Fee.",
    )
    psf = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="PSF",
        help_text="Passenger Service Fee.",
    )
    asf = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="ASF",
        help_text="Aviation Security Fee.",
    )
    gst = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="GST",
        help_text="Goods and Services Tax (5% economy / 12% premium cabins).",
    )
    other_charges = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    taxes_fees = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="UDF + PSF + ASF + GST + other, when every part is known.",
    )
    total_fare = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="All-in payable fare. NULL when inventory is unpriced.",
    )

    # -- inventory / quality ------------------------------------------------ #
    inventory_status = models.CharField(
        max_length=20, choices=InventoryStatus.choices,
        default=InventoryStatus.AVAILABLE, db_index=True,
    )
    seats_remaining = models.PositiveSmallIntegerField(null=True, blank=True)
    is_refundable = models.BooleanField(null=True, blank=True)
    fare_basis = models.CharField(max_length=32, blank=True, default="")
    booking_class = models.CharField(max_length=4, blank=True, default="")

    is_modeled = models.BooleanField(
        default=False, db_index=True,
        help_text="True when the value was imputed rather than directly observed.",
    )
    quality_grade = models.CharField(
        max_length=1, choices=QualityGrade.choices, default=QualityGrade.B_TOTAL_ONLY, db_index=True,
    )
    flags = ArrayField(
        models.CharField(max_length=32, choices=FareFlag.choices), default=list, blank=True,
    )

    # -- outlier screening -------------------------------------------------- #
    is_outlier = models.BooleanField(default=False, db_index=True)
    modified_z_score = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
        help_text="0.6745 * (x - median) / MAD, computed within the stratum.",
    )
    outlier_stratum = models.CharField(max_length=96, blank=True, default="")

    # -- deduplication ------------------------------------------------------ #
    fingerprint = models.CharField(
        max_length=64, db_index=True, validators=[SHA256_VALIDATOR],
        help_text="SHA-256 of the flight identity: route, carrier, flight, departure, cabin, window.",
    )
    content_hash = models.CharField(
        max_length=64, validators=[SHA256_VALIDATOR],
        help_text="SHA-256 of identity + every monetary field; equal hashes are exact duplicates.",
    )
    is_duplicate = models.BooleanField(default=False, db_index=True)
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates",
    )
    dedup_reason = models.CharField(max_length=32, blank=True, default="")

    provenance = models.JSONField(
        default=dict, blank=True,
        help_text="Per-stage cleaning lineage: parser notes, audit flags, node digests.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects: ClassVar[CanonicalFareQuerySet] = CanonicalFareQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        db_table = "apix_canonical_fare"
        ordering = ("-observed_at", "route_id", "carrier_iata")
        verbose_name = "canonical fare"
        constraints = [
            # One record per (source, flight identity, observation) - a re-run
            # of the same collection must be idempotent.
            models.UniqueConstraint(
                fields=("source", "fingerprint", "observation_date"),
                name="uq_fare_source_fingerprint_day",
            ),
            # Missing != Zero, enforced by the database itself.
            models.CheckConstraint(
                condition=Q(total_fare__isnull=True) | Q(total_fare__gt=Decimal("0")),
                name="ck_fare_total_positive_or_null",
            ),
            models.CheckConstraint(
                condition=(
                    Q(inventory_status=InventoryStatus.AVAILABLE, total_fare__isnull=False)
                    | ~Q(inventory_status=InventoryStatus.AVAILABLE)
                ),
                name="ck_fare_available_requires_price",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(inventory_status=InventoryStatus.SOLD_OUT) | Q(total_fare__isnull=True)
                ),
                name="ck_fare_sold_out_has_null_total",
            ),
            models.CheckConstraint(
                condition=Q(base_fare__isnull=True) | Q(base_fare__gte=Decimal("0")),
                name="ck_fare_base_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(lead_window_days__gte=0) & Q(lead_window_days__lte=365),
                name="ck_fare_lead_window_range",
            ),
        ]
        indexes = [
            # The workhorse: "give me every eligible fare for this route and
            # window over this observation range" - the index compiler's query.
            models.Index(
                fields=("route", "lead_window_days", "observation_date"),
                name="ix_fare_route_window_day",
            ),
            models.Index(fields=("observation_date", "quality_grade"), name="ix_fare_day_quality"),
            models.Index(fields=("fingerprint", "observation_date"), name="ix_fare_fingerprint_day"),
            models.Index(fields=("carrier_iata", "-observed_at"), name="ix_fare_carrier_observed"),
            models.Index(
                fields=("route", "lead_window_days", "-observed_at"),
                condition=Q(is_duplicate=False, is_outlier=False, total_fare__isnull=False),
                name="ix_fare_index_eligible",
            ),
        ]

    def __str__(self) -> str:
        price = self.total_fare if self.total_fare is not None else "NULL"
        return f"{self.route_id} {self.carrier_iata}{self.flight_number} T+{self.lead_window_days} = {price}"

    @property
    def is_priced(self) -> bool:
        return self.total_fare is not None and self.total_fare > Decimal("0")

    @property
    def has_full_breakdown(self) -> bool:
        return all(
            value is not None for value in (self.base_fare, self.udf, self.psf, self.asf, self.gst)
        )

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags = [*self.flags, flag]


# --------------------------------------------------------------------------- #
# Published statistics
# --------------------------------------------------------------------------- #
class IndexValueQuerySet(QuerySet["IndexValue"]):
    def published(self) -> Self:
        return self.filter(is_published=True)

    def national(self) -> Self:
        """The composite series: no route dimension."""
        return self.filter(route__isnull=True)

    def series(self, *, methodology: str, lead_window_days: int | None, route_id: int | None) -> Self:
        return self.filter(
            methodology_code=methodology,
            lead_window_days=lead_window_days,
            route_id=route_id,
        ).order_by("index_date")


class IndexValue(models.Model):
    """One published index level.

    ``route`` NULL denotes the **national composite** - the DGCA-weighted
    Laspeyres aggregation across the route basket.  A non-NULL ``route`` is the
    elementary Jevons aggregate for that city-pair.  ``lead_window_days`` NULL
    denotes the all-window composite.

    ``provenance_hash`` seals the value; ``previous_hash`` chains it to the
    prior print in the same series.  Verifying the chain proves that no
    historical value has been silently restated.
    """

    id = models.BigAutoField(primary_key=True)

    methodology_code = models.CharField(
        max_length=32, choices=MethodologyCode.choices,
        default=MethodologyCode.APIX_JEVONS_V1, db_index=True,
    )
    index_date = models.DateField(db_index=True, help_text="IST calendar day the index refers to.")
    lead_window_days = models.PositiveSmallIntegerField(
        null=True, blank=True, db_index=True, help_text="NULL = composite across all windows.",
    )
    route = models.ForeignKey(
        Route, null=True, blank=True, on_delete=models.PROTECT, related_name="index_values",
        help_text="NULL = national composite across the DGCA basket.",
    )

    index_value = models.DecimalField(
        max_digits=20, decimal_places=8,
        help_text="Base period = 100. Jevons at route level, Laspeyres at national level.",
    )
    base_period = models.CharField(max_length=16, default="2025-04")
    base_index_value = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal("100"))

    # -- diagnostics published alongside the level -------------------------- #
    mean_log_relative = models.DecimalField(
        max_digits=20, decimal_places=12, null=True, blank=True,
        help_text="(1/n) * SUM ln(p_t/p_0) - the log-space quantity that was exponentiated.",
    )
    period_on_period_pct = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    year_on_year_pct = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    n_observations = models.IntegerField(default=0)
    n_matched_pairs = models.IntegerField(default=0)
    n_routes = models.IntegerField(default=0)
    n_outliers_excluded = models.IntegerField(default=0)
    weight_coverage = models.DecimalField(
        max_digits=12, decimal_places=10, null=True, blank=True,
        help_text="Share of the DGCA weight base actually represented by data.",
    )

    # -- provenance --------------------------------------------------------- #
    provenance_hash = models.CharField(
        max_length=64, unique=True, validators=[SHA256_VALIDATOR],
        help_text="SHA-256 sealing this value, its inputs and its methodology.",
    )
    previous_hash = models.CharField(
        max_length=64, blank=True, default="", help_text="Provenance hash of the prior print in this series.",
    )
    inputs_root = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Merkle root over the raw ingress hashes that fed this value.",
    )
    raw_observation_count = models.IntegerField(default=0)
    computation_metadata = models.JSONField(default=dict, blank=True)

    is_published = models.BooleanField(default=False, db_index=True)
    suppression_reason = models.TextField(blank=True, default="")
    computed_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[IndexValueQuerySet] = IndexValueQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        db_table = "apix_index_value"
        ordering = ("-index_date", "route_id", "lead_window_days")
        verbose_name = "index value"
        constraints = [
            models.UniqueConstraint(
                fields=("methodology_code", "index_date", "lead_window_days", "route"),
                name="uq_index_series_point",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=Q(index_value__gt=Decimal("0")), name="ck_index_value_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=("methodology_code", "route", "lead_window_days", "index_date"),
                name="ix_index_series_lookup",
            ),
            models.Index(fields=("is_published", "-index_date"), name="ix_index_published_date"),
        ]

    def __str__(self) -> str:
        scope = self.route.code if self.route_id else "NATIONAL"
        window = f"T+{self.lead_window_days}" if self.lead_window_days else "ALL"
        return f"{scope} {window} {self.index_date}: {self.index_value}"

    @property
    def scope(self) -> str:
        return self.route.code if self.route_id else "NATIONAL"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
class AuditLog(models.Model):
    """Append-only record of every consequential system or human action."""

    id = models.BigAutoField(primary_key=True)
    action = models.CharField(max_length=40, choices=AuditAction.choices, db_index=True)
    actor = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries",
    )
    actor_label = models.CharField(
        max_length=64, default="system", help_text="'system', 'celery:<task>' or a username.",
    )
    entity_type = models.CharField(max_length=64, blank=True, default="", db_index=True)
    entity_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    summary = models.CharField(max_length=255, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    context = models.JSONField(default=dict, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "apix_audit_log"
        ordering = ("-created_at",)
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log"
        indexes = [
            models.Index(fields=("entity_type", "entity_id", "-created_at"), name="ix_audit_entity"),
            models.Index(fields=("action", "-created_at"), name="ix_audit_action_time"),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.entity_type}:{self.entity_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk is not None:
            raise ValidationError("Audit log entries are append-only.")
        super().save(*args, **kwargs)

    @classmethod
    def record(
        cls,
        action: str,
        *,
        summary: str = "",
        entity: models.Model | None = None,
        actor: Any = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        correlation_id: str = "",
    ) -> AuditLog:
        """Convenience writer used across services and admin actions."""
        return cls.objects.create(
            action=action,
            actor=actor if getattr(actor, "pk", None) else None,
            actor_label=getattr(actor, "username", None) or "system",
            entity_type=type(entity).__name__ if entity is not None else "",
            entity_id=str(getattr(entity, "pk", "")) if entity is not None else "",
            summary=summary[:255],
            before=before,
            after=after,
            context=context or {},
            correlation_id=correlation_id,
        )
