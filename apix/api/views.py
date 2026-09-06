"""DRF views.

Caching policy
--------------
Published index values are immutable once sealed, so they are cached
aggressively (15 minutes) and keyed by the full query string.  Fare listings are
cached briefly (60 seconds) because a collection run can land mid-afternoon.
Anything unpublished, suppressed or operational is never cached - an analyst
looking at a suppressed value must see the current truth.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from django.core.cache import cache
from django.db.models import Count, Max, Min, Q, QuerySet
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apix.api.filters import (
    AuditLogFilter,
    CanonicalFareFilter,
    CollectionRunFilter,
    IndexValueFilter,
    RawObservationFilter,
)
from apix.api.pagination import CursorTimeseriesPagination, SeriesPagination
from apix.api.permissions import (
    CanReadRawPayloads,
    CanTriggerCollection,
    IsAdminRole,
    IsNSOAnalyst,
    IsStatisticalReader,
    roles_for,
)
from apix.api.serializers import (
    AuditLogSerializer,
    CanonicalFareSerializer,
    CollectionRunSerializer,
    CollectionTriggerResponseSerializer,
    CollectionTriggerSerializer,
    FareSummarySerializer,
    IndexPointSerializer,
    IndexValueSerializer,
    LeadTimeCurveSerializer,
    ProvenanceVerificationSerializer,
    RawObservationDetailSerializer,
    RawObservationSerializer,
    RouteSerializer,
    SourceResumeSerializer,
    SourceSerializer,
)
from apix.enums import InventoryStatus, QualityGrade, UserRole
from apix.models import (
    AuditLog,
    CanonicalFare,
    CollectionRun,
    IndexValue,
    RawObservation,
    Route,
    Source,
)
from apix.policies.engine import get_policy_engine
from apix.services.indexer import JevonsCalculator
from apix.services.provenance import verify_series
from apix.tasks import dispatch_daily_basket

logger = logging.getLogger(__name__)

_INDEX_CACHE_SECONDS = 900
_FARE_CACHE_SECONDS = 60
_ANALYTICS_CACHE_SECONDS = 600


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
@extend_schema_view(
    list=extend_schema(summary="List routes in the sampling frame", tags=["reference"]),
    retrieve=extend_schema(summary="Retrieve one route", tags=["reference"]),
)
class RouteViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Route.objects.all().prefetch_related("weights")
    serializer_class = RouteSerializer
    permission_classes = (IsStatisticalReader,)
    pagination_class = None
    filterset_fields = ("is_active", "is_in_basket", "origin", "destination")


@extend_schema_view(
    list=extend_schema(summary="List sources and their compliance state", tags=["operations"]),
)
class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Source.objects.select_related("policy").all()
    serializer_class = SourceSerializer
    permission_classes = (IsNSOAnalyst,)
    pagination_class = None

    @extend_schema(
        summary="Live compliance snapshot (bucket level, breaker, robots)",
        tags=["operations"],
    )
    @action(detail=True, methods=["get"], url_path="compliance")
    def compliance(self, request: Request, pk: str | None = None) -> Response:
        source = self.get_object()
        return Response(get_policy_engine().snapshot(source.code))

    @extend_schema(
        summary="Resume a halted source (operator action, audited)",
        request=SourceResumeSerializer,
        tags=["operations"],
    )
    @action(detail=True, methods=["post"], url_path="resume", permission_classes=(IsAdminRole,))
    def resume(self, request: Request, pk: str | None = None) -> Response:
        """Clear a breaker.

        A source halted by a bot challenge requires ``force=true`` **and** a
        written justification, both recorded in the audit log.  The platform
        will not quietly resume collection from a host that challenged us.
        """
        source = self.get_object()
        serializer = SourceResumeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        resumed = get_policy_engine().resume(
            source.code,
            actor=request.user,
            force=serializer.validated_data["force"],
            note=serializer.validated_data["note"],
        )
        if not resumed:
            return Response(
                {
                    "detail": (
                        "Source is halted by a permanent trip (bot challenge). "
                        "Re-submit with force=true and a justification after confirming "
                        "with the source that automated access is permitted."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(get_policy_engine().snapshot(source.code))


# --------------------------------------------------------------------------- #
# Index series
# --------------------------------------------------------------------------- #
@extend_schema_view(
    list=extend_schema(summary="Index series", tags=["index"]),
    retrieve=extend_schema(summary="One index value with its provenance", tags=["index"]),
)
class IndexValueViewSet(viewsets.ReadOnlyModelViewSet):
    """``/api/v1/index/`` - published Jevons/Laspeyres levels."""

    serializer_class = IndexValueSerializer
    permission_classes = (IsStatisticalReader,)
    filterset_class = IndexValueFilter
    pagination_class = SeriesPagination
    ordering = ("-index_date",)
    ordering_fields = ("index_date", "index_value", "lead_window_days")
    throttle_scope = "index_read"

    def get_queryset(self) -> QuerySet[IndexValue]:
        queryset = IndexValue.objects.select_related("route").all()
        # Suppressed values are an internal judgement; RBI researchers see only
        # what the NSO chose to publish.
        if not (roles_for(self.request.user) & {UserRole.ADMIN, UserRole.NSO_ANALYST}):
            queryset = queryset.published()
        return queryset

    @extend_schema(
        summary="Latest published value for every lead window",
        parameters=[
            OpenApiParameter("route", str, description="Route code; omit for the national composite."),
        ],
        tags=["index"],
    )
    @method_decorator(cache_page(_INDEX_CACHE_SECONDS))
    @method_decorator(vary_on_headers("Authorization"))
    @action(detail=False, methods=["get"], url_path="latest")
    def latest(self, request: Request) -> Response:
        """The headline print: newest published level per lead window."""
        route_code = request.query_params.get("route")
        base = self.get_queryset().published()
        base = base.filter(route__code__iexact=route_code) if route_code else base.filter(route__isnull=True)

        payload: list[dict[str, Any]] = []
        for window in sorted({row for row in base.values_list("lead_window_days", flat=True) if row}):
            newest = base.filter(lead_window_days=window).order_by("-index_date").first()
            if newest:
                payload.append(IndexValueSerializer(newest).data)

        return Response({
            "scope": route_code.upper() if route_code else "NATIONAL",
            "as_of": max((row["index_date"] for row in payload), default=None),
            "values": payload,
        })

    @extend_schema(
        summary="Compact time series for charting",
        parameters=[
            OpenApiParameter("route", str, description="Route code; omit for national."),
            OpenApiParameter("lead_window", int, description="1, 7, 15, 30 or 45."),
            OpenApiParameter("date_from", str), OpenApiParameter("date_to", str),
        ],
        tags=["index"],
    )
    @method_decorator(cache_page(_INDEX_CACHE_SECONDS))
    @method_decorator(vary_on_headers("Authorization"))
    @action(detail=False, methods=["get"], url_path="series")
    def series(self, request: Request) -> Response:
        route_code = request.query_params.get("route")
        window = request.query_params.get("lead_window")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        queryset = self.get_queryset().published()
        queryset = (
            queryset.filter(route__code__iexact=route_code) if route_code
            else queryset.filter(route__isnull=True)
        )
        if window:
            queryset = queryset.filter(lead_window_days=int(window))
        if date_from:
            queryset = queryset.filter(index_date__gte=date.fromisoformat(date_from))
        if date_to:
            queryset = queryset.filter(index_date__lte=date.fromisoformat(date_to))

        points = queryset.order_by("index_date")
        return Response({
            "scope": route_code.upper() if route_code else "NATIONAL",
            "lead_window_days": int(window) if window else None,
            "base_period": points.values_list("base_period", flat=True).first(),
            "methodology_code": points.values_list("methodology_code", flat=True).first(),
            "count": points.count(),
            "points": IndexPointSerializer(points, many=True).data,
        })

    @extend_schema(
        summary="Verify the SHA-256 provenance chain of a series",
        responses=ProvenanceVerificationSerializer,
        tags=["index"],
    )
    @action(detail=False, methods=["get"], url_path="verify", permission_classes=(IsNSOAnalyst,))
    def verify(self, request: Request) -> Response:
        """Recompute every seal and every back-link in a published series."""
        route_code = request.query_params.get("route")
        window = request.query_params.get("lead_window")
        queryset = IndexValue.objects.published().order_by("index_date")
        queryset = (
            queryset.filter(route__code__iexact=route_code) if route_code
            else queryset.filter(route__isnull=True)
        )
        if window:
            queryset = queryset.filter(lead_window_days=int(window))
        verification = verify_series(list(queryset))
        return Response(verification.as_dict())


# --------------------------------------------------------------------------- #
# Fares
# --------------------------------------------------------------------------- #
@extend_schema_view(
    list=extend_schema(summary="Cleaned fare observations", tags=["fares"]),
)
class CanonicalFareViewSet(viewsets.ReadOnlyModelViewSet):
    """``/api/v1/fares/`` - the cleaned, deduplicated observation layer."""

    serializer_class = CanonicalFareSerializer
    permission_classes = (IsStatisticalReader,)
    filterset_class = CanonicalFareFilter
    pagination_class = CursorTimeseriesPagination
    throttle_scope = "fares_read"

    def get_queryset(self) -> QuerySet[CanonicalFare]:
        return (
            CanonicalFare.objects.select_related("route", "source")
            .survivors()
            .order_by("-observed_at")
        )

    @extend_schema(
        summary="Most recent observation day, with an inline summary",
        parameters=[
            OpenApiParameter("route", str), OpenApiParameter("lead_window", int),
            OpenApiParameter("quality", str, description="A, B, C or D."),
        ],
        tags=["fares"],
    )
    @action(detail=False, methods=["get"], url_path="latest")
    def latest(self, request: Request) -> Response:
        """Latest fares plus the counts an analyst always asks for next.

        ``sold_out`` is reported separately from ``priced`` precisely because
        those rows carry ``total_fare = null`` - they are observations of
        scarcity, not observations of a zero price.
        """
        queryset = self.filter_queryset(self.get_queryset())
        newest_day = queryset.aggregate(day=Max("observation_date"))["day"]
        if newest_day is None:
            return Response({"observation_date": None, "summary": FareSummarySerializer.zero(), "results": []})

        day_rows = queryset.filter(observation_date=newest_day)
        page = self.paginate_queryset(day_rows)
        summary = self._summarise(day_rows)
        body = {
            "observation_date": newest_day,
            "summary": summary,
            "results": CanonicalFareSerializer(page if page is not None else day_rows, many=True).data,
        }
        if page is not None:
            paginated = self.get_paginated_response(body["results"])
            paginated.data["observation_date"] = newest_day
            paginated.data["summary"] = summary
            return paginated
        return Response(body)

    @staticmethod
    def _summarise(queryset: QuerySet[CanonicalFare]) -> dict[str, Any]:
        aggregate = queryset.aggregate(
            observations=Count("id"),
            priced=Count("id", filter=Q(total_fare__isnull=False)),
            sold_out=Count("id", filter=Q(inventory_status=InventoryStatus.SOLD_OUT)),
            outliers=Count("id", filter=Q(is_outlier=True)),
            partial=Count("id", filter=Q(quality_grade=QualityGrade.B_TOTAL_ONLY)),
            min_fare=Min("total_fare"),
            max_fare=Max("total_fare"),
        )
        fares = list(
            queryset.filter(total_fare__isnull=False).values_list("total_fare", flat=True)
        )
        median = _median_decimal(fares)
        return {
            "observations": aggregate["observations"],
            "priced": aggregate["priced"],
            "sold_out": aggregate["sold_out"],
            "outliers": aggregate["outliers"],
            "partial_breakdown": aggregate["partial"],
            "min_fare": str(aggregate["min_fare"]) if aggregate["min_fare"] is not None else None,
            "max_fare": str(aggregate["max_fare"]) if aggregate["max_fare"] is not None else None,
            "median_fare": str(median) if median is not None else None,
        }


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
class LeadTimeCurveView(APIView):
    """``/api/v1/analytics/lead-time-curve/`` - the empirical booking curve.

    Shows what a passenger actually pays as a function of days to departure.
    Quantiles use NumPy on ``float`` because they are *descriptive reporting*
    statistics; the geometric mean - which feeds the reader's intuition about
    the index itself - is computed in ``Decimal`` with the same log-space code
    path the Jevons compiler uses, so the two never disagree.
    """

    permission_classes = (IsStatisticalReader,)
    throttle_scope = "analytics"

    @extend_schema(
        summary="Fare distribution by booking lead time",
        parameters=[
            OpenApiParameter("route", str, description="Route code; omit for the whole basket."),
            OpenApiParameter("days", int, description="Look-back window in days (default 30)."),
            OpenApiParameter("carrier", str),
        ],
        responses=LeadTimeCurveSerializer(many=True),
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response:
        route_code = (request.query_params.get("route") or "").upper()
        carrier = (request.query_params.get("carrier") or "").upper()
        days = min(int(request.query_params.get("days") or 30), 365)
        since = date.today() - timedelta(days=days)

        cache_key = f"apix:leadcurve:{route_code}:{carrier}:{days}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        queryset = CanonicalFare.objects.survivors().filter(observation_date__gte=since)
        if route_code:
            queryset = queryset.filter(route__code=route_code)
        if carrier:
            queryset = queryset.filter(carrier_iata=carrier)

        jevons = JevonsCalculator()
        curve: list[dict[str, Any]] = []

        for window in sorted(set(queryset.values_list("lead_window_days", flat=True))):
            cell = queryset.filter(lead_window_days=window)
            priced = [
                Decimal(value)
                for value in cell.filter(total_fare__isnull=False).values_list("total_fare", flat=True)
            ]
            total_rows = cell.count()
            sold_out = cell.filter(inventory_status=InventoryStatus.SOLD_OUT).count()

            if priced:
                array = np.array([float(value) for value in priced], dtype=np.float64)
                p25, p50, p75 = (Decimal(str(round(v, 2))) for v in np.percentile(array, [25, 50, 75]))
                geometric = jevons.geometric_mean(priced).quantize(Decimal("0.0001"))
            else:
                p25 = p50 = p75 = geometric = None

            curve.append({
                "lead_window_days": window,
                "route_code": route_code or None,
                "observations": total_rows,
                "median_fare": str(p50) if p50 is not None else None,
                "geometric_mean_fare": str(geometric) if geometric is not None else None,
                "p25_fare": str(p25) if p25 is not None else None,
                "p75_fare": str(p75) if p75 is not None else None,
                "sold_out_share": (
                    str((Decimal(sold_out) / Decimal(total_rows)).quantize(Decimal("0.0001")))
                    if total_rows else None
                ),
                "index_value": self._index_for(window, route_code),
            })

        payload = {"route": route_code or "ALL", "lookback_days": days, "curve": curve}
        cache.set(cache_key, payload, _ANALYTICS_CACHE_SECONDS)
        return Response(payload)

    @staticmethod
    def _index_for(window: int, route_code: str) -> str | None:
        queryset = IndexValue.objects.published().filter(lead_window_days=window)
        queryset = (
            queryset.filter(route__code=route_code) if route_code else queryset.filter(route__isnull=True)
        )
        row = queryset.order_by("-index_date").values_list("index_value", flat=True).first()
        return str(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
class CollectionTriggerView(APIView):
    """``POST /api/v1/collection/trigger/`` - dispatch an asynchronous run."""

    permission_classes = (CanTriggerCollection,)
    throttle_scope = "collection_trigger"

    @extend_schema(
        summary="Trigger a collection run",
        request=CollectionTriggerSerializer,
        responses=CollectionTriggerResponseSerializer,
        tags=["operations"],
    )
    def post(self, request: Request) -> Response:
        serializer = CollectionTriggerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        run_id = dispatch_daily_basket(
            route_codes=data.get("route_codes"),
            lead_windows=data.get("lead_windows"),
            source_codes=data.get("source_codes"),
            triggered_by_id=request.user.pk,
            reason=data.get("reason", "manual"),
        )
        run = CollectionRun.objects.get(pk=run_id)
        logger.info(
            "collection triggered",
            extra={"run_id": run_id, "user": request.user.get_username()},
        )
        return Response(
            {
                "run_id": run_id,
                "status": run.status,
                "task_count": len(run.parameters.get("route_codes") or []) or run.routes.count(),
                "detail": "Collection queued. Poll /api/v1/collection/runs/{run_id}/ for progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema_view(
    list=extend_schema(summary="Collection runs", tags=["operations"]),
    retrieve=extend_schema(summary="One run with per-stage statistics", tags=["operations"]),
)
class CollectionRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CollectionRun.objects.prefetch_related("routes").select_related("triggered_by")
    serializer_class = CollectionRunSerializer
    permission_classes = (IsNSOAnalyst,)
    filterset_class = CollectionRunFilter
    pagination_class = SeriesPagination
    ordering = ("-created_at",)


@extend_schema_view(
    list=extend_schema(summary="Raw payload index (metadata only)", tags=["operations"]),
    retrieve=extend_schema(summary="One immutable raw payload", tags=["operations"]),
)
class RawObservationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RawObservation.objects.select_related("source", "route")
    permission_classes = (CanReadRawPayloads,)
    filterset_class = RawObservationFilter
    pagination_class = CursorTimeseriesPagination

    def get_serializer_class(self):  # noqa: ANN201
        return RawObservationDetailSerializer if self.action == "retrieve" else RawObservationSerializer


@extend_schema_view(list=extend_schema(summary="Audit log", tags=["operations"]))
class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.select_related("actor")
    serializer_class = AuditLogSerializer
    permission_classes = (IsNSOAnalyst,)
    filterset_class = AuditLogFilter
    pagination_class = SeriesPagination
    ordering = ("-created_at",)


class WhoAmIView(APIView):
    """Lets the React console render navigation without decoding the JWT."""

    permission_classes = (IsAuthenticated,)

    @extend_schema(summary="Current user and roles", tags=["auth"])
    def get(self, request: Request) -> Response:
        return Response({
            "username": request.user.get_username(),
            "name": request.user.get_full_name() or request.user.get_username(),
            "roles": sorted(roles_for(request.user)),
            "is_staff": request.user.is_staff,
        })


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _median_decimal(values: list[Decimal]) -> Decimal | None:
    """Exact median in ``Decimal`` - no float round trip for a money value."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return ((ordered[mid - 1] + ordered[mid]) / Decimal(2)).quantize(Decimal("0.01"))
