"""API v1 routing.

::

    /api/v1/auth/token/                 POST   obtain access + refresh
    /api/v1/auth/token/refresh/         POST   rotate
    /api/v1/auth/me/                    GET    identity and roles

    /api/v1/index/                      GET    filterable index values
    /api/v1/index/latest/               GET    headline print per lead window
    /api/v1/index/series/               GET    compact series for charting
    /api/v1/index/verify/               GET    recompute the provenance chain

    /api/v1/fares/                      GET    cleaned observations
    /api/v1/fares/latest/               GET    newest day + inline summary

    /api/v1/analytics/lead-time-curve/  GET    empirical booking curve

    /api/v1/collection/trigger/         POST   dispatch a Celery run
    /api/v1/collection/runs/            GET    run history and statistics
    /api/v1/sources/                    GET    registry + compliance state
    /api/v1/sources/{id}/compliance/    GET    live bucket / breaker snapshot
    /api/v1/sources/{id}/resume/        POST   audited operator resume
    /api/v1/raw/                        GET    immutable payload index
    /api/v1/audit/                      GET    audit log
"""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from apix.api.views import (
    AuditLogViewSet,
    CanonicalFareViewSet,
    CollectionRunViewSet,
    CollectionTriggerView,
    IndexValueViewSet,
    LeadTimeCurveView,
    RawObservationViewSet,
    RouteViewSet,
    SourceViewSet,
    WhoAmIView,
)

app_name = "apix"

router = DefaultRouter()
router.register("index", IndexValueViewSet, basename="index")
router.register("fares", CanonicalFareViewSet, basename="fares")
router.register("routes", RouteViewSet, basename="routes")
router.register("sources", SourceViewSet, basename="sources")
router.register("raw", RawObservationViewSet, basename="raw")
router.register("audit", AuditLogViewSet, basename="audit")
router.register("collection/runs", CollectionRunViewSet, basename="collection-runs")

urlpatterns = [
    # -- auth -------------------------------------------------------------- #
    path("auth/token/", TokenObtainPairView.as_view(), name="token-obtain"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token-verify"),
    path("auth/me/", WhoAmIView.as_view(), name="whoami"),

    # -- analytics --------------------------------------------------------- #
    path("analytics/lead-time-curve/", LeadTimeCurveView.as_view(), name="lead-time-curve"),

    # -- operations -------------------------------------------------------- #
    path("collection/trigger/", CollectionTriggerView.as_view(), name="collection-trigger"),

    # -- resources --------------------------------------------------------- #
    path("", include(router.urls)),
]
