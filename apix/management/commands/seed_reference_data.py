"""Seed the reference tables: routes, DGCA weights, sources, RBAC groups.

Idempotent - safe to run on every deploy.  Passenger volumes below are
illustrative placeholders shaped like DGCA domestic city-pair traffic; replace
them with the published figures before the index goes live.  The command
deliberately writes them as a *versioned* :class:`~apix.models.RouteWeight` row
with ``valid_from``, so the real numbers supersede rather than overwrite these.

Usage::

    python manage.py seed_reference_data
    python manage.py seed_reference_data --with-demo-users
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from apix.enums import SourceKind, UserRole
from apix.models import Route, RouteWeight, Source, SourcePolicy

#: (code, origin, destination, origin city, destination city, km, illustrative pax)
BASKET: tuple[tuple[str, str, str, str, str, int, int], ...] = (
    ("DEL-BOM", "DEL", "BOM", "Delhi", "Mumbai", 1148, 5_100_000),
    ("DEL-BLR", "DEL", "BLR", "Delhi", "Bengaluru", 1740, 3_900_000),
    ("BOM-BLR", "BOM", "BLR", "Mumbai", "Bengaluru", 842, 2_800_000),
    ("DEL-CCU", "DEL", "CCU", "Delhi", "Kolkata", 1305, 2_100_000),
    ("BLR-HYD", "BLR", "HYD", "Bengaluru", "Hyderabad", 500, 1_600_000),
    ("MAA-DEL", "MAA", "DEL", "Chennai", "Delhi", 1760, 1_900_000),
)

SOURCES: tuple[dict[str, Any], ...] = (
    {
        "code": "mock_feed",
        "name": "Deterministic synthetic feed",
        "kind": SourceKind.MOCK_FEED,
        "base_url": "https://mock.apix.local",
        "trust_rank": 90,
        "requests_per_minute": 600,
        "burst": 60,
        "respect_robots": False,
    },
    {
        "code": "permitted_web_json",
        "name": "Permitted web source (configure before enabling)",
        "kind": SourceKind.PERMITTED_WEB,
        "base_url": "",
        "trust_rank": 30,
        "requests_per_minute": 20,
        "burst": 3,
        "respect_robots": True,
        "is_active": False,
        "attributes": {
            "search_url_template": "",
            "fare_response_pattern": "/api/",
            "results_path": [],
            "field_map": {},
        },
    },
)


class Command(BaseCommand):
    help = "Seed routes, DGCA weight versions, sources and RBAC groups."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--with-demo-users", action="store_true",
            help="Create one demo user per role (local development only).",
        )
        parser.add_argument(
            "--weight-period", default="FY2024-25",
            help="Reference period label for the seeded DGCA weight version.",
        )
        parser.add_argument(
            "--valid-from", default="2025-04-01",
            help="ISO date the seeded weight version takes effect.",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        valid_from = date.fromisoformat(options["valid_from"])
        period = options["weight_period"]

        routes = self._seed_routes()
        self._seed_weights(routes, period=period, valid_from=valid_from)
        self._seed_sources()
        self._seed_groups()
        if options["with_demo_users"]:
            self._seed_demo_users()

        self.stdout.write(self.style.SUCCESS("Reference data seeded."))

    # -- routes ------------------------------------------------------------- #
    def _seed_routes(self) -> dict[str, Route]:
        routes: dict[str, Route] = {}
        for code, origin, destination, origin_city, destination_city, km, _pax in BASKET:
            route, created = Route.objects.update_or_create(
                origin=origin,
                destination=destination,
                defaults={
                    "origin_city": origin_city,
                    "destination_city": destination_city,
                    "distance_km": km,
                    "is_active": True,
                    "is_in_basket": True,
                },
            )
            routes[code] = route
            self.stdout.write(f"  route {code} {'created' if created else 'updated'}")
        return routes

    # -- weights ------------------------------------------------------------ #
    def _seed_weights(self, routes: dict[str, Route], *, period: str, valid_from: date) -> None:
        """Write one versioned weight row per route, shares normalised to 1."""
        total = Decimal(sum(row[6] for row in BASKET))

        for code, _o, _d, _oc, _dc, _km, passengers in BASKET:
            share = (Decimal(passengers) / total).quantize(Decimal("0.0000000001"))
            _, created = RouteWeight.objects.update_or_create(
                route=routes[code],
                valid_from=valid_from,
                defaults={
                    "valid_to": None,
                    "passengers": passengers,
                    "share": share,
                    "reference_period": period,
                    "source_document": "DGCA domestic traffic statistics (placeholder)",
                    "published_on": valid_from,
                },
            )
            self.stdout.write(
                f"  weight {code} {period}: share={share} {'created' if created else 'updated'}"
            )

    # -- sources ------------------------------------------------------------ #
    def _seed_sources(self) -> None:
        for spec in SOURCES:
            policy_fields = {
                "requests_per_minute": spec.pop("requests_per_minute", 30),
                "burst": spec.pop("burst", 5),
                "respect_robots": spec.pop("respect_robots", True),
            }
            code = spec.pop("code")
            source, created = Source.objects.update_or_create(code=code, defaults=spec)
            # ``ensure_source_policy`` created the row on insert; align the knobs.
            SourcePolicy.objects.update_or_create(
                source=source,
                defaults={
                    **policy_fields,
                    "robots_url": f"{source.base_url.rstrip('/')}/robots.txt" if source.base_url else "",
                },
            )
            self.stdout.write(f"  source {code} {'created' if created else 'updated'}")

    # -- RBAC --------------------------------------------------------------- #
    def _seed_groups(self) -> None:
        for role in UserRole.values:
            group, created = Group.objects.get_or_create(name=role)
            self.stdout.write(f"  group {group.name} {'created' if created else 'exists'}")

    def _seed_demo_users(self) -> None:
        demo = {
            "apix_admin": UserRole.ADMIN,
            "nso_analyst": UserRole.NSO_ANALYST,
            "rbi_researcher": UserRole.RBI_RESEARCHER,
        }
        for username, role in demo.items():
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@apix.local", "is_staff": role == UserRole.ADMIN},
            )
            if created:
                user.set_password("apix-demo-password")
                user.is_superuser = role == UserRole.ADMIN
                user.save()
            user.groups.add(Group.objects.get(name=role))
            self.stdout.write(
                self.style.WARNING(f"  demo user {username} ({role}) - password 'apix-demo-password'")
            )
