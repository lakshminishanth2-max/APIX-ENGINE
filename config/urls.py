"""Root URL configuration."""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


def healthz(_: HttpRequest) -> JsonResponse:
    """Liveness probe - no database access, must stay trivially fast."""
    return JsonResponse({"status": "ok", "service": "apix-core"})


def readyz(_: HttpRequest) -> JsonResponse:
    """Readiness probe - verifies PostgreSQL and Redis are actually reachable."""
    from django.core.cache import cache
    from django.db import connection

    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - probe must report, not raise
        checks["postgres"] = f"error: {exc}"
    try:
        cache.set("apix:readyz", "1", 10)
        checks["redis"] = "ok" if cache.get("apix:readyz") == "1" else "error: no round trip"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"

    healthy = all(value == "ok" for value in checks.values())
    return JsonResponse({"status": "ready" if healthy else "degraded", "checks": checks},
                        status=200 if healthy else 503)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("api/v1/", include(("apix.api.urls", "apix"), namespace="v1")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

if settings.DEBUG:  # pragma: no cover - developer convenience only
    urlpatterns += [path("api-auth/", include("rest_framework.urls"))]

admin.site.site_header = "APIx - Airfare Price Index Administration"
admin.site.site_title = "APIx Admin"
admin.site.index_title = "National Statistical Office - Airfare Price Index"
