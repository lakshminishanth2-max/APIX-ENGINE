"""DRF serializers.

Two conventions run through every serializer here:

* **Decimals stay strings.**  ``COERCE_DECIMAL_TO_STRING`` is on globally.  A
  JSON number is an IEEE-754 double the moment JavaScript parses it, and an
  index level that round-trips through a double is no longer the value that was
  hashed into the provenance chain.  The React client parses the string only for
  charting, never for arithmetic it displays.
* **NULL is transmitted as null.**  ``total_fare: null`` on a sold-out flight is
  the payload, not an omission to be tidied into ``0``.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import AbstractUser
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apix.api.permissions import roles_for
from apix.enums import CollectionStatus
from apix.models import (
    AuditLog,
    CanonicalFare,
    CollectionRun,
    IndexValue,
    RawObservation,
    Route,
    RouteWeight,
    Source,
    SourcePolicy,
)

__all__ = [
    "APIxTokenObtainPairSerializer",
    "CanonicalFareSerializer",
    "CollectionRunSerializer",
    "CollectionTriggerSerializer",
    "IndexValueSerializer",
    "LeadTimeCurveSerializer",
    "RawObservationSerializer",
    "RouteSerializer",
    "RouteWeightSerializer",
    "SourceSerializer",
]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class APIxTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Embeds roles and display name in the access token."""

    @classmethod
    def get_token(cls, user: AbstractUser) -> Any:  # type: ignore[override]
        token = super().get_token(user)
        token["roles"] = sorted(roles_for(user))
        token["name"] = user.get_full_name() or user.get_username()
        token["is_staff"] = user.is_staff
        return token

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        data = super().validate(attrs)
        data["roles"] = sorted(roles_for(self.user))
        data["username"] = self.user.get_username()
        data["name"] = self.user.get_full_name() or self.user.get_username()
        return data


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
class RouteSerializer(serializers.ModelSerializer):
    current_weight = serializers.SerializerMethodField()

    class Meta:
        model = Route
        fields = (
            "id", "code", "origin", "destination", "origin_city", "destination_city",
            "distance_km", "is_active", "is_in_basket", "current_weight",
        )
        read_only_fields = fields

    def get_current_weight(self, obj: Route) -> str | None:
        weight = getattr(obj, "current_weight_cache", None)
        if weight is None:
            weight = obj.weights.filter(valid_to__isnull=True).order_by("-valid_from").first()
        return str(weight.share) if weight else None


class RouteWeightSerializer(serializers.ModelSerializer):
    route_code = serializers.CharField(source="route.code", read_only=True)

    class Meta:
        model = RouteWeight
        fields = (
            "id", "route", "route_code", "valid_from", "valid_to", "passengers",
            "share", "reference_period", "source_document", "published_on",
        )
        read_only_fields = ("id", "route_code")


class SourcePolicySerializer(serializers.ModelSerializer):
    is_collectable = serializers.BooleanField(read_only=True)

    class Meta:
        model = SourcePolicy
        fields = (
            "requests_per_minute", "burst", "max_concurrency", "robots_url",
            "respect_robots", "robots_last_checked", "robots_last_verdict",
            "crawl_delay_seconds", "compliance_status", "circuit_state",
            "consecutive_failures", "halted_at", "halt_reason", "last_success_at",
            "total_requests", "total_failures", "is_collectable",
        )
        read_only_fields = (
            "robots_last_checked", "robots_last_verdict", "circuit_state",
            "consecutive_failures", "halted_at", "halt_reason", "last_success_at",
            "total_requests", "total_failures", "is_collectable",
        )


class SourceSerializer(serializers.ModelSerializer):
    policy = SourcePolicySerializer(read_only=True)

    class Meta:
        model = Source
        fields = (
            "id", "code", "name", "kind", "carrier_iata", "base_url",
            "trust_rank", "is_active", "policy",
        )
        read_only_fields = ("id",)


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #
class CanonicalFareSerializer(serializers.ModelSerializer):
    """A cleaned fare.  Nullable money fields are transmitted as ``null``."""

    route_code = serializers.CharField(source="route.code", read_only=True)
    source_code = serializers.CharField(source="source.code", read_only=True)
    quality_grade_display = serializers.CharField(source="get_quality_grade_display", read_only=True)
    is_priced = serializers.BooleanField(read_only=True)

    class Meta:
        model = CanonicalFare
        fields = (
            "id", "route", "route_code", "source", "source_code",
            "carrier_iata", "flight_number", "cabin",
            "departure_utc", "departure_local", "observed_at", "observation_date",
            "lead_window_days", "currency",
            "base_fare", "udf", "psf", "asf", "gst", "other_charges",
            "taxes_fees", "total_fare",
            "inventory_status", "seats_remaining", "is_refundable",
            "quality_grade", "quality_grade_display", "flags",
            "is_outlier", "modified_z_score", "is_duplicate", "is_modeled",
            "is_priced", "fingerprint",
        )
        read_only_fields = fields


class CanonicalFareCompactSerializer(serializers.ModelSerializer):
    """Trimmed projection for chart endpoints - far cheaper over the wire."""

    route_code = serializers.CharField(source="route.code", read_only=True)

    class Meta:
        model = CanonicalFare
        fields = (
            "route_code", "carrier_iata", "lead_window_days", "observation_date",
            "total_fare", "inventory_status", "is_outlier", "quality_grade",
        )
        read_only_fields = fields


class RawObservationSerializer(serializers.ModelSerializer):
    source_code = serializers.CharField(source="source.code", read_only=True)
    route_code = serializers.CharField(source="route.code", read_only=True, default=None)

    class Meta:
        model = RawObservation
        fields = (
            "id", "ingress_hash", "source", "source_code", "route", "route_code",
            "lead_window_days", "search_departure_date", "payload_kind",
            "request_url", "http_status", "content_bytes", "captured_at",
            "collector", "is_processed", "processed_at", "processing_error",
        )
        read_only_fields = fields


class RawObservationDetailSerializer(RawObservationSerializer):
    """Includes the verbatim payload; restricted to NSO analysts and admins."""

    class Meta(RawObservationSerializer.Meta):
        fields = (*RawObservationSerializer.Meta.fields, "payload", "response_headers")
        read_only_fields = fields


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
class IndexValueSerializer(serializers.ModelSerializer):
    scope = serializers.CharField(read_only=True)
    route_code = serializers.CharField(source="route.code", read_only=True, default=None)

    class Meta:
        model = IndexValue
        fields = (
            "id", "methodology_code", "index_date", "lead_window_days",
            "route", "route_code", "scope", "index_value", "base_period",
            "base_index_value", "period_on_period_pct", "year_on_year_pct",
            "n_observations", "n_matched_pairs", "n_routes", "n_outliers_excluded",
            "weight_coverage", "provenance_hash", "previous_hash", "inputs_root",
            "raw_observation_count", "is_published", "suppression_reason",
            "computed_at", "published_at",
        )
        read_only_fields = fields


class IndexPointSerializer(serializers.ModelSerializer):
    """Minimal shape for charting a long series."""

    class Meta:
        model = IndexValue
        fields = ("index_date", "index_value", "period_on_period_pct", "year_on_year_pct", "n_matched_pairs")
        read_only_fields = fields


class LeadTimeCurveSerializer(serializers.Serializer):
    """One point of the booking-curve analytic."""

    lead_window_days = serializers.IntegerField()
    route_code = serializers.CharField(required=False, allow_null=True)
    observations = serializers.IntegerField()
    median_fare = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    geometric_mean_fare = serializers.DecimalField(max_digits=14, decimal_places=4, allow_null=True)
    p25_fare = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    p75_fare = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    sold_out_share = serializers.DecimalField(max_digits=6, decimal_places=4, allow_null=True)
    index_value = serializers.DecimalField(max_digits=20, decimal_places=8, allow_null=True)


class ProvenanceVerificationSerializer(serializers.Serializer):
    scope = serializers.CharField()
    verified = serializers.IntegerField()
    intact = serializers.BooleanField()
    broken_at = serializers.CharField(allow_null=True)
    reason = serializers.CharField(allow_null=True)


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
class CollectionRunSerializer(serializers.ModelSerializer):
    triggered_by_username = serializers.CharField(source="triggered_by.username", read_only=True, default=None)
    route_codes = serializers.SerializerMethodField()
    duration_seconds = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = CollectionRun
        fields = (
            "id", "status", "trigger_reason", "triggered_by_username", "celery_task_id",
            "correlation_id", "route_codes", "lead_windows", "parameters",
            "scheduled_for", "started_at", "finished_at", "duration_seconds",
            "raw_count", "canonical_count", "duplicate_count", "outlier_count",
            "rejected_count", "stats", "error", "created_at",
        )
        read_only_fields = fields

    def get_route_codes(self, obj: CollectionRun) -> list[str]:
        return sorted(route.code for route in obj.routes.all())


class CollectionTriggerSerializer(serializers.Serializer):
    """Request body for ``POST /api/v1/collection/trigger/``."""

    route_codes = serializers.ListField(
        child=serializers.CharField(max_length=7), required=False, allow_empty=False,
        help_text="Defaults to the full DGCA basket.",
    )
    lead_windows = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=365), required=False, allow_empty=False,
        help_text="Defaults to [1, 7, 15, 30, 45].",
    )
    source_codes = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, allow_empty=False,
    )
    reason = serializers.CharField(max_length=64, required=False, default="manual")

    def validate_route_codes(self, value: list[str]) -> list[str]:
        codes = [code.strip().upper() for code in value]
        known = set(Route.objects.filter(code__in=codes).values_list("code", flat=True))
        unknown = sorted(set(codes) - known)
        if unknown:
            raise serializers.ValidationError(f"unknown route codes: {', '.join(unknown)}")
        return codes

    def validate_source_codes(self, value: list[str]) -> list[str]:
        codes = [code.strip().lower() for code in value]
        known = set(Source.objects.filter(code__in=codes).values_list("code", flat=True))
        unknown = sorted(set(codes) - known)
        if unknown:
            raise serializers.ValidationError(f"unknown source codes: {', '.join(unknown)}")
        return codes


class CollectionTriggerResponseSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=CollectionStatus.choices)
    task_count = serializers.IntegerField()
    detail = serializers.CharField()


class SourceResumeSerializer(serializers.Serializer):
    """Operator action to clear a halted source."""

    force = serializers.BooleanField(
        default=False,
        help_text="Required to clear a permanent trip caused by a bot challenge.",
    )
    note = serializers.CharField(
        max_length=500, required=True,
        help_text="Mandatory justification - written to the audit log.",
    )


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = (
            "id", "action", "actor_label", "entity_type", "entity_id",
            "summary", "context", "correlation_id", "created_at",
        )
        read_only_fields = fields


class FareSummarySerializer(serializers.Serializer):
    """Aggregate returned alongside a fare list, so the UI needs one call."""

    observations = serializers.IntegerField()
    priced = serializers.IntegerField()
    sold_out = serializers.IntegerField()
    outliers = serializers.IntegerField()
    partial_breakdown = serializers.IntegerField()
    min_fare = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    max_fare = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    median_fare = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)

    @staticmethod
    def zero() -> dict[str, Any]:
        return {
            "observations": 0, "priced": 0, "sold_out": 0, "outliers": 0,
            "partial_breakdown": 0, "min_fare": None, "max_fare": None, "median_fare": None,
        }
