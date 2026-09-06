"""Request middleware."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apix.logging_utils import bind_correlation_id

HEADER = "HTTP_X_CORRELATION_ID"
RESPONSE_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware:
    """Accepts or mints a correlation id and binds it for the request's life.

    The same id is passed as a Celery header when a request dispatches a
    collection job, so a trigger in the React console and the resulting
    compliance decisions, cleaning report and provenance seal all share one key.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        correlation_id = request.META.get(HEADER) or uuid.uuid4().hex
        request.correlation_id = correlation_id  # type: ignore[attr-defined]
        bind_correlation_id(correlation_id)
        try:
            response = self.get_response(request)
        finally:
            bind_correlation_id(None)
        response[RESPONSE_HEADER] = correlation_id
        return response
