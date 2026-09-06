"""FactoryBoy factories.

Factories build *valid* objects by default.  Every pathological case the
cleaning pipeline exists to handle - a sold-out flight, an OTA that publishes
only a total, a mis-parsed price - is produced by an explicit trait, so a test
that exercises one is unmistakable in the test body.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import factory
from django.contrib.auth.models import Group, User
from factory.django import DjangoModelFactory

from apix.enums import (
    CabinClass,
    CollectionStatus,
    InventoryStatus,
    MethodologyCode,
    QualityGrade,
    SourceKind,
)
from apix.models import (
    CanonicalFare,
    CollectionRun,
    IndexValue,
    RawObservation,
    Route,
    RouteWeight,
    Source,
    SourcePolicy,
)

UTC = timezone.utc


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)

    username = factory.Sequence(lambda n: f"analyst{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@apix.local")
    is_active = True

    @factory.post_generation
    def role(obj: User, create: bool, extracted: str | None, **kwargs: object) -> None:
        if create and extracted:
            obj.groups.add(Group.objects.get_or_create(name=extracted)[0])


class SourceFactory(DjangoModelFactory):
    class Meta:
        model = Source
        django_get_or_create = ("code",)

    code = "mock_feed"
    name = "Deterministic synthetic feed"
    kind = SourceKind.MOCK_FEED
    base_url = "https://mock.apix.local"
    trust_rank = 90
    is_active = True


class SourcePolicyFactory(DjangoModelFactory):
    class Meta:
        model = SourcePolicy
        django_get_or_create = ("source",)

    source = factory.SubFactory(SourceFactory)
    requests_per_minute = 600
    burst = 60
    respect_robots = False


class RouteFactory(DjangoModelFactory):
    class Meta:
        model = Route
        django_get_or_create = ("origin", "destination")

    origin = "DEL"
    destination = "BOM"
    origin_city = "Delhi"
    destination_city = "Mumbai"
    distance_km = 1148
    is_active = True
    is_in_basket = True


class RouteWeightFactory(DjangoModelFactory):
    class Meta:
        model = RouteWeight

    route = factory.SubFactory(RouteFactory)
    valid_from = date(2025, 4, 1)
    valid_to = None
    passengers = 5_100_000
    share = Decimal("0.3000000000")
    reference_period = "FY2024-25"
    source_document = "DGCA domestic traffic statistics (test fixture)"
    published_on = date(2025, 4, 1)


class CollectionRunFactory(DjangoModelFactory):
    class Meta:
        model = CollectionRun

    status = CollectionStatus.SUCCEEDED
    trigger_reason = "test"
    lead_windows = [1, 7, 15, 30, 45]


class RawObservationFactory(DjangoModelFactory):
    class Meta:
        model = RawObservation

    ingress_hash = factory.Sequence(lambda n: f"{n:064x}")
    source = factory.SubFactory(SourceFactory)
    route = factory.SubFactory(RouteFactory)
    lead_window_days = 7
    search_departure_date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    payload = factory.LazyFunction(dict)
    request_url = "https://mock.apix.local/v1/search"
    http_status = 200
    captured_at = factory.LazyFunction(lambda: datetime.now(UTC))
    collector = "mock_feed"


class CanonicalFareFactory(DjangoModelFactory):
    """A clean, fully-broken-down, index-eligible economy fare."""

    class Meta:
        model = CanonicalFare

    raw_observation = factory.SubFactory(RawObservationFactory)
    source = factory.SubFactory(SourceFactory)
    route = factory.SubFactory(RouteFactory)
    carrier_iata = "6E"
    flight_number = factory.Sequence(lambda n: f"6E{2000 + n}")
    cabin = CabinClass.ECONOMY
    departure_utc = factory.LazyFunction(lambda: datetime.now(UTC) + timedelta(days=7))
    observed_at = factory.LazyFunction(lambda: datetime.now(UTC))
    observation_date = factory.LazyFunction(date.today)
    lead_window_days = 7
    currency = "INR"
    base_fare = Decimal("4200.00")
    udf = Decimal("250.00")
    psf = Decimal("175.00")
    asf = Decimal("236.00")
    gst = Decimal("210.00")
    other_charges = Decimal("0.00")
    taxes_fees = Decimal("871.00")
    total_fare = Decimal("5071.00")
    inventory_status = InventoryStatus.AVAILABLE
    quality_grade = QualityGrade.A_FULL_BREAKDOWN
    flags = factory.LazyFunction(list)
    fingerprint = factory.Sequence(lambda n: f"{n:064x}")
    content_hash = factory.Sequence(lambda n: f"{n + 10**6:064x}")

    class Params:
        #: Sold out: the row exists, the price does not.  NULL, never zero.
        sold_out = factory.Trait(
            inventory_status=InventoryStatus.SOLD_OUT,
            total_fare=None,
            base_fare=None,
            udf=None,
            psf=None,
            asf=None,
            gst=None,
            other_charges=None,
            taxes_fees=None,
            quality_grade=QualityGrade.D_UNUSABLE,
            flags=["SOLD_OUT"],
        )
        #: An OTA that publishes only an all-in price.
        partial_breakdown = factory.Trait(
            base_fare=None,
            udf=None,
            psf=None,
            asf=None,
            gst=None,
            other_charges=None,
            taxes_fees=None,
            quality_grade=QualityGrade.B_TOTAL_ONLY,
            flags=["PARTIAL_BREAKDOWN"],
        )
        outlier = factory.Trait(
            total_fare=Decimal("48000.00"),
            is_outlier=True,
            modified_z_score=Decimal("7.412000"),
            quality_grade=QualityGrade.C_SUSPECT,
            flags=["OUTLIER_HIGH"],
        )
        duplicate = factory.Trait(is_duplicate=True, dedup_reason="lost_tie_break")


class IndexValueFactory(DjangoModelFactory):
    class Meta:
        model = IndexValue

    methodology_code = MethodologyCode.APIX_JEVONS_V1
    index_date = factory.LazyFunction(date.today)
    lead_window_days = 7
    route = None
    index_value = Decimal("100.00000000")
    base_period = "2025-04"
    provenance_hash = factory.Sequence(lambda n: f"{n + 10**9:064x}")
    is_published = True
