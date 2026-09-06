"""Query filters for the analytical endpoints.

Every filter maps onto a composite index defined in :mod:`apix.models`; the
combinations exposed here are precisely the ones the schema was designed to
serve, which keeps analyst queries off sequential scans.
"""

from __future__ import annotations

from django_filters import rest_framework as filters

from apix.enums import CabinClass, InventoryStatus, MethodologyCode, QualityGrade
from apix.models import AuditLog, CanonicalFare, CollectionRun, IndexValue, RawObservation

__all__ = [
    "AuditLogFilter",
    "CanonicalFareFilter",
    "CollectionRunFilter",
    "IndexValueFilter",
    "RawObservationFilter",
]


class CanonicalFareFilter(filters.FilterSet):
    """Filters for ``/api/v1/fares/``."""

    route = filters.CharFilter(field_name="route__code", lookup_expr="iexact")
    route_in = filters.BaseInFilter(field_name="route__code", lookup_expr="in")
    source = filters.CharFilter(field_name="source__code", lookup_expr="iexact")
    carrier = filters.CharFilter(field_name="carrier_iata", lookup_expr="iexact")
    lead_window = filters.NumberFilter(field_name="lead_window_days")
    lead_window_in = filters.BaseInFilter(field_name="lead_window_days", lookup_expr="in")
    cabin = filters.ChoiceFilter(choices=CabinClass.choices)
    quality = filters.ChoiceFilter(field_name="quality_grade", choices=QualityGrade.choices)
    quality_in = filters.BaseInFilter(field_name="quality_grade", lookup_expr="in")
    inventory_status = filters.ChoiceFilter(choices=InventoryStatus.choices)

    observed_from = filters.DateFilter(field_name="observation_date", lookup_expr="gte")
    observed_to = filters.DateFilter(field_name="observation_date", lookup_expr="lte")
    departure_from = filters.DateTimeFilter(field_name="departure_utc", lookup_expr="gte")
    departure_to = filters.DateTimeFilter(field_name="departure_utc", lookup_expr="lte")

    min_fare = filters.NumberFilter(field_name="total_fare", lookup_expr="gte")
    max_fare = filters.NumberFilter(field_name="total_fare", lookup_expr="lte")

    is_outlier = filters.BooleanFilter()
    is_duplicate = filters.BooleanFilter()
    priced_only = filters.BooleanFilter(method="filter_priced_only")
    index_eligible = filters.BooleanFilter(method="filter_index_eligible")

    class Meta:
        model = CanonicalFare
        fields: list[str] = []

    def filter_priced_only(self, queryset, name, value):  # noqa: ANN001, ANN201
        """``true`` excludes sold-out and unpriced rows (``total_fare IS NULL``)."""
        return queryset.priced() if value else queryset

    def filter_index_eligible(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.index_eligible() if value else queryset


class IndexValueFilter(filters.FilterSet):
    """Filters for ``/api/v1/index/series/``."""

    methodology = filters.ChoiceFilter(field_name="methodology_code", choices=MethodologyCode.choices)
    route = filters.CharFilter(field_name="route__code", lookup_expr="iexact")
    national = filters.BooleanFilter(method="filter_national")
    lead_window = filters.NumberFilter(field_name="lead_window_days")
    lead_window_in = filters.BaseInFilter(field_name="lead_window_days", lookup_expr="in")
    date_from = filters.DateFilter(field_name="index_date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="index_date", lookup_expr="lte")
    published = filters.BooleanFilter(field_name="is_published")

    class Meta:
        model = IndexValue
        fields: list[str] = []

    def filter_national(self, queryset, name, value):  # noqa: ANN001, ANN201
        """``true`` selects the DGCA-weighted composite (``route IS NULL``)."""
        return queryset.filter(route__isnull=True) if value else queryset.filter(route__isnull=False)


class RawObservationFilter(filters.FilterSet):
    source = filters.CharFilter(field_name="source__code", lookup_expr="iexact")
    route = filters.CharFilter(field_name="route__code", lookup_expr="iexact")
    lead_window = filters.NumberFilter(field_name="lead_window_days")
    captured_from = filters.DateTimeFilter(field_name="captured_at", lookup_expr="gte")
    captured_to = filters.DateTimeFilter(field_name="captured_at", lookup_expr="lte")
    is_processed = filters.BooleanFilter()
    has_error = filters.BooleanFilter(method="filter_has_error")

    class Meta:
        model = RawObservation
        fields: list[str] = []

    def filter_has_error(self, queryset, name, value):  # noqa: ANN001, ANN201
        return queryset.exclude(processing_error="") if value else queryset.filter(processing_error="")


class CollectionRunFilter(filters.FilterSet):
    status = filters.CharFilter(lookup_expr="iexact")
    created_from = filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = CollectionRun
        fields: list[str] = []


class AuditLogFilter(filters.FilterSet):
    action = filters.CharFilter(lookup_expr="iexact")
    entity_type = filters.CharFilter(lookup_expr="iexact")
    entity_id = filters.CharFilter(lookup_expr="iexact")
    created_from = filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = AuditLog
        fields: list[str] = []
