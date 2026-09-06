"""Shared pytest fixtures.

Tests split into two tiers:

* **pure** - the statistical and parsing code, which touches no database and
  runs in milliseconds.  These are the tests that guard the index arithmetic and
  they must stay fast enough to run on every save.
* **integration** (``@pytest.mark.django_db``) - the pipeline and API, which
  need PostgreSQL because the schema depends on ``ARRAY`` columns, ``JSONB`` and
  partial indexes that SQLite cannot express.
"""

from __future__ import annotations

import os
from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import Any
from collections.abc import Iterator

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _deterministic_decimal_context() -> Iterator[None]:
    """Pin precision and rounding so a test never depends on import order."""
    from decimal import ROUND_HALF_EVEN, getcontext

    context = getcontext()
    previous = (context.prec, context.rounding)
    context.prec, context.rounding = 34, ROUND_HALF_EVEN
    yield
    context.prec, context.rounding = previous


@pytest.fixture
def observation_date() -> date:
    return date(2026, 9, 1)


@pytest.fixture
def collection_task(observation_date: date) -> Any:
    from apix.collectors.base import CollectionTask

    return CollectionTask(
        route_code="DEL-BOM",
        origin="DEL",
        destination="BOM",
        departure_date=observation_date + timedelta(days=7),
        lead_window_days=7,
        observation_date=observation_date,
    )


@pytest.fixture
def mock_collector() -> Any:
    from apix.collectors.mock import MockCollector

    return MockCollector(base_url="https://mock.apix.local")


@pytest.fixture
def route(db: Any) -> Any:
    from apix.tests.factories import RouteFactory

    return RouteFactory()


@pytest.fixture
def source(db: Any) -> Any:
    from apix.tests.factories import SourceFactory, SourcePolicyFactory

    created = SourceFactory()
    SourcePolicyFactory(source=created)
    return created


@pytest.fixture
def weighted_basket(db: Any) -> list[Any]:
    """The six DGCA basket routes with normalised weight versions."""
    from apix.tests.factories import RouteFactory, RouteWeightFactory

    definitions = [
        ("DEL", "BOM", 5_100_000), ("DEL", "BLR", 3_900_000), ("BOM", "BLR", 2_800_000),
        ("DEL", "CCU", 2_100_000), ("BLR", "HYD", 1_600_000), ("MAA", "DEL", 1_900_000),
    ]
    total = Decimal(sum(pax for *_pair, pax in definitions))
    routes = []
    for origin, destination, passengers in definitions:
        created = RouteFactory(origin=origin, destination=destination)
        RouteWeightFactory(
            route=created,
            passengers=passengers,
            share=(Decimal(passengers) / total).quantize(Decimal("0.0000000001")),
        )
        routes.append(created)
    return routes


@pytest.fixture
def api_client() -> Any:
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def nso_client(db: Any, api_client: Any) -> Any:
    """Authenticated client holding the NSO_ANALYST role."""
    from apix.enums import UserRole
    from apix.tests.factories import UserFactory

    user = UserFactory(role=UserRole.NSO_ANALYST)
    api_client.force_authenticate(user=user)
    api_client.handler._force_user = user  # type: ignore[attr-defined]
    return api_client
