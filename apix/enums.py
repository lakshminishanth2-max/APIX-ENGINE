"""Domain vocabulary.

These enumerations are the contract between the collection tier, the cleaning
pipeline, the statistical compiler and the API.  They are stored as short
strings rather than integers so a DBA reading the table, or an RBI analyst
reading a CSV export, sees the meaning without a lookup table.
"""

from __future__ import annotations

from django.db import models

__all__ = [
    "AuditAction",
    "CabinClass",
    "CollectionStatus",
    "ComplianceStatus",
    "CircuitState",
    "FareFlag",
    "InventoryStatus",
    "MethodologyCode",
    "PayloadKind",
    "QualityGrade",
    "SourceKind",
    "UserRole",
]


class SourceKind(models.TextChoices):
    """How a source is reached, which decides which collector drives it."""

    DIRECT_API = "DIRECT_API", "Direct carrier / GDS API (contracted)"
    PERMITTED_WEB = "PERMITTED_WEB", "Public web page, robots-permitted"
    MOCK_FEED = "MOCK_FEED", "Deterministic synthetic feed"


class ComplianceStatus(models.TextChoices):
    """Operational permission state of a source."""

    ACTIVE = "ACTIVE", "Active"
    PAUSED = "PAUSED", "Paused by operator"
    HALTED_BOT_CHALLENGE = "HALTED_BOT_CHALLENGE", "Halted - bot challenge detected"
    HALTED_ROBOTS = "HALTED_ROBOTS", "Halted - robots.txt disallows"
    DISABLED = "DISABLED", "Disabled"

    @classmethod
    def blocking(cls) -> tuple[str, ...]:
        return (cls.PAUSED, cls.HALTED_BOT_CHALLENGE, cls.HALTED_ROBOTS, cls.DISABLED)


class CircuitState(models.TextChoices):
    """Circuit-breaker position for a source."""

    CLOSED = "CLOSED", "Closed - traffic flowing"
    OPEN = "OPEN", "Open - cooling down"
    HALF_OPEN = "HALF_OPEN", "Half open - probing"
    #: Terminal.  Only an operator can clear this; no timer will.
    TRIPPED_PERMANENT = "TRIPPED_PERMANENT", "Tripped - operator reset required"


class PayloadKind(models.TextChoices):
    JSON = "JSON", "JSON"
    HTML = "HTML", "HTML"
    XML = "XML", "XML"
    CSV = "CSV", "CSV"


class CabinClass(models.TextChoices):
    ECONOMY = "ECONOMY", "Economy"
    PREMIUM_ECONOMY = "PREMIUM_ECONOMY", "Premium economy"
    BUSINESS = "BUSINESS", "Business"
    FIRST = "FIRST", "First"


class InventoryStatus(models.TextChoices):
    """Why a fare does or does not carry a price.

    ``SOLD_OUT`` and ``PRICE_UNAVAILABLE`` observations are *kept* - they are
    real economic signals about scarcity - but they carry ``total_fare = NULL``.
    A zero would multiply into the Jevons geometric mean and collapse the whole
    elementary aggregate to zero.
    """

    AVAILABLE = "AVAILABLE", "Available - priced"
    SOLD_OUT = "SOLD_OUT", "Sold out - no price"
    NOT_OFFERED = "NOT_OFFERED", "Flight not offered on this date"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE", "Offered but price not published"

    @classmethod
    def priceable(cls) -> tuple[str, ...]:
        return (cls.AVAILABLE,)


class QualityGrade(models.TextChoices):
    """Fitness of a record for statistical use, coarsest first.

    Grade drives inclusion: only A and B enter the headline index; C is held for
    diagnostics; D never enters a published aggregate.
    """

    A_FULL_BREAKDOWN = "A", "A - full component breakdown, reconciles to total"
    B_TOTAL_ONLY = "B", "B - trustworthy total, partial or absent breakdown"
    C_SUSPECT = "C", "C - suspect (mismatch, outlier, stale)"
    D_UNUSABLE = "D", "D - unusable (unpriced, modelled or failed validation)"

    @classmethod
    def index_eligible(cls) -> tuple[str, ...]:
        return (cls.A_FULL_BREAKDOWN, cls.B_TOTAL_ONLY)


class FareFlag(models.TextChoices):
    """Non-exclusive annotations attached to a canonical fare."""

    PARTIAL_BREAKDOWN = "PARTIAL_BREAKDOWN", "Total present, components missing"
    COMPONENT_DERIVED = "COMPONENT_DERIVED", "One component derived as residual"
    TOTAL_DERIVED = "TOTAL_DERIVED", "Total derived from components"
    TOTAL_MISMATCH = "TOTAL_MISMATCH", "Components do not reconcile to total"
    EXPLICIT_ZERO = "EXPLICIT_ZERO", "A component is genuinely zero (waived)"
    SOLD_OUT = "SOLD_OUT", "Inventory exhausted - fare is NULL by rule"
    OUTLIER_HIGH = "OUTLIER_HIGH", "Modified Z-score above threshold"
    OUTLIER_LOW = "OUTLIER_LOW", "Modified Z-score below negative threshold"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED", "Lost cross-source dedup tie-break"
    CROSS_SOURCE_CONFLICT = "CROSS_SOURCE_CONFLICT", "Sources disagree on price"
    STALE_OBSERVATION = "STALE_OBSERVATION", "Observed outside the collection window"
    MODELLED = "MODELLED", "Imputed / modelled, not directly observed"
    COMPONENT_UNPARSEABLE = "COMPONENT_UNPARSEABLE", "A component value was corrupt or unparseable"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE", "Offered but price not published"


class CollectionStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    PARTIAL = "PARTIAL", "Partially succeeded"
    FAILED = "FAILED", "Failed"
    BLOCKED = "BLOCKED", "Blocked by compliance policy"
    CANCELLED = "CANCELLED", "Cancelled"

    @classmethod
    def terminal(cls) -> tuple[str, ...]:
        return (cls.SUCCEEDED, cls.PARTIAL, cls.FAILED, cls.BLOCKED, cls.CANCELLED)


class MethodologyCode(models.TextChoices):
    """Versioned statistical methodology.

    A published :class:`~apix.models.IndexValue` is permanently bound to the
    code it was computed under.  Changing a formula or a parameter means minting
    a new code - never mutating an existing series in place.
    """

    APIX_JEVONS_V1 = "APIX_JEVONS_V1", "Trimmed Jevons elementary + DGCA Laspeyres (v1)"
    APIX_JEVONS_V2 = "APIX_JEVONS_V2", "Trimmed Jevons elementary + DGCA Laspeyres (v2)"


class AuditAction(models.TextChoices):
    SOURCE_HALTED = "SOURCE_HALTED", "Source halted"
    SOURCE_RESUMED = "SOURCE_RESUMED", "Source resumed by operator"
    BOT_CHALLENGE_DETECTED = "BOT_CHALLENGE_DETECTED", "Bot challenge detected"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED", "robots.txt disallowed a fetch"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED", "Rate limit wait exceeded"
    COLLECTION_TRIGGERED = "COLLECTION_TRIGGERED", "Collection triggered"
    COLLECTION_COMPLETED = "COLLECTION_COMPLETED", "Collection completed"
    CLEANING_COMPLETED = "CLEANING_COMPLETED", "Cleaning completed"
    INDEX_COMPILED = "INDEX_COMPILED", "Index compiled"
    INDEX_PUBLISHED = "INDEX_PUBLISHED", "Index published"
    INDEX_SUPPRESSED = "INDEX_SUPPRESSED", "Index suppressed (coverage/quality)"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE", "Manual override applied"
    WEIGHT_VERSION_ADDED = "WEIGHT_VERSION_ADDED", "DGCA weight version added"


class UserRole(models.TextChoices):
    """RBAC roles, mirrored as Django groups and embedded in the JWT."""

    ADMIN = "ADMIN", "Administrator"
    NSO_ANALYST = "NSO_ANALYST", "NSO analyst"
    RBI_RESEARCHER = "RBI_RESEARCHER", "RBI researcher"
