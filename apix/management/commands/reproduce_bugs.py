"""Reproduce the open cleaning-pipeline defects, on demand, with no database.

    python manage.py reproduce_bugs

Each check runs the real pipeline stages against one crafted input and prints
what actually happens versus what should happen.  When a defect is fixed, its
check flips to PASS - so this doubles as the acceptance test for the fixes.

Full write-ups: BUGS-AND-DEPENDENCIES.md, and APIx-Research-Paper.md §10.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand

from apix.enums import FareFlag, QualityGrade
from apix.models import Route, Source
from apix.services.cleaning import FareCleaningPipeline, StagedFare

UTC = timezone.utc

BASE_ROW: dict[str, Any] = {
    "carrier_iata": "6E",
    "flight_number": "6E2134",
    "departure_local": "2026-09-08T06:15:00",
    "cabin": "economy",
    "currency": "INR",
}


class Command(BaseCommand):
    help = "Reproduce the open cleaning defects (no database required)."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover
            pass

        self.pipeline = FareCleaningPipeline()
        self.route = Route(origin="DEL", destination="BOM", code="DEL-BOM")
        self.source = Source(code="mock_feed", trust_rank=90)

        self.stdout.write("")
        self.stdout.write("═" * 74)
        self.stdout.write("  APIx  ·  OPEN DEFECT REPRODUCTION")
        self.stdout.write("═" * 74)

        results = [
            self.bug_1_duplicate_fingerprint(),
            self.bug_3_zero_price(),
            self.bug_4_sold_out_without_time(),
            self.bug_5_corrupt_component(),
        ]

        self.stdout.write("")
        self.stdout.write("─" * 74)
        failed = sum(1 for ok in results if not ok)
        if failed:
            self.stdout.write(self.style.ERROR(
                f"  {failed} of {len(results)} defects still present."
            ))
            self.stdout.write("  Bug 2 (failed saves marked done) and bug 7 (unordered duplicate")
            self.stdout.write("  linkage) need a live database and are not checked here.")
        else:
            self.stdout.write(self.style.SUCCESS(
                f"  All {len(results)} checks pass - these defects are fixed."
            ))
        self.stdout.write("")

    # ------------------------------------------------------------------ #
    def bug_1_duplicate_fingerprint(self) -> bool:
        self._header(1, "Two ticket types on one flight share an ID", "CRITICAL")

        saver = self._stage({**BASE_ROW, "fare_basis": "Q07SAVER",
                             "booking_class": "Q", "total_fare": "4521.00"})
        flexi = self._stage({**BASE_ROW, "fare_basis": "M07FLEXI",
                             "booking_class": "M", "total_fare": "7899.00"})

        if saver is None or flexi is None:
            return self._verdict(False, "rows unexpectedly rejected")

        self._line("Saver  ₹4,521.00", saver.fingerprint)
        self._line("Flexi  ₹7,899.00", flexi.fingerprint)

        collides = saver.fingerprint == flexi.fingerprint
        if collides:
            self.stdout.write("      → identical IDs, different prices")
            self.stdout.write("      → PostgreSQL: 'ON CONFLICT DO UPDATE command cannot")
            self.stdout.write("        affect row a second time' — the whole batch is lost")
        return self._verdict(not collides, "fingerprint omits fare_basis / booking_class")

    # ------------------------------------------------------------------ #
    def bug_3_zero_price(self) -> bool:
        self._header(3, "A price of ₹0 is accepted as a real fare", "HIGH")

        fare = self._stage({**BASE_ROW, "total_fare": "0"})
        if fare is None:
            return self._verdict(True, "row correctly rejected before persist")

        self._line("total_fare", repr(fare.total_fare))
        self._line("inventory_status", str(fare.inventory_status))
        self._line("quality_grade", str(fare.quality_grade))

        accepted = fare.total_fare is not None and fare.total_fare <= Decimal("0")
        if accepted:
            self.stdout.write("      → DB rule: total_fare IS NULL OR total_fare > 0")
            self.stdout.write("      → this row violates it and aborts the whole batch")
        return self._verdict(not accepted, "zero is never checked; only None is")

    # ------------------------------------------------------------------ #
    def bug_4_sold_out_without_time(self) -> bool:
        self._header(4, "Sold-out flights without a departure time vanish", "MEDIUM")

        fare = self._stage({
            "carrier_iata": "6E", "flight_number": "6E2134",
            "cabin": "economy", "inventory_status": "SOLD_OUT",
        })

        if fare is None:
            self.stdout.write("      row dropped at stage 3 (no parseable departure time)")
            self.stdout.write("      → the scarcity signal is discarded, and it is densest")
            self.stdout.write("        at T+1 where sites most often omit times")
        else:
            self._line("inventory_status", str(fare.inventory_status))
            self._line("total_fare", "NULL" if fare.total_fare is None else str(fare.total_fare))
        return self._verdict(fare is not None, "time is required before inventory is inspected")

    # ------------------------------------------------------------------ #
    def bug_5_corrupt_component(self) -> bool:
        self._header(5, "Corrupt values are recorded as 'not published'", "MEDIUM")

        fare = self._stage({**BASE_ROW, "base_fare": "-500.00", "total_fare": "4521.00"})
        if fare is None:
            return self._verdict(False, "row unexpectedly rejected")

        self._line("base_fare", "NULL" if fare.base_fare is None else str(fare.base_fare))
        self._line("quality_grade", str(fare.quality_grade))
        self._line("flags", ", ".join(str(f) for f in fare.flags) or "(none)")

        mislabelled = (
            fare.base_fare is None
            and FareFlag.PARTIAL_BREAKDOWN in fare.flags
            and fare.quality_grade == QualityGrade.B_TOTAL_ONLY
        )
        if mislabelled:
            self.stdout.write("      → the site DID publish a value; it was garbage (-500)")
            self.stdout.write("      → recorded as 'site was silent', grade B, no penalty")
        return self._verdict(not mislabelled, "no COMPONENT_UNPARSEABLE flag, no grade penalty")

    # ------------------------------------------------------------------ #
    def _stage(self, row: dict[str, Any]) -> StagedFare | None:
        """Run cleaning stages 2-5 over one crafted row."""
        fare = StagedFare(
            raw_observation_id=1, source=self.source, route=self.route,
            lead_window_days=7, observation_date=date(2026, 9, 1),
            observed_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
        )
        if not self.pipeline._stage_2_currency(dict(row), fare):
            return None
        if not self.pipeline._stage_3_canonicalise(dict(row), fare):
            return None
        if not self.pipeline._stage_4_validate(fare):
            return None
        self.pipeline._stage_5_audit_components(fare)
        fare.fingerprint = self.pipeline._fingerprint(fare)
        fare.content_hash = self.pipeline._content_hash(fare)
        return fare

    def _header(self, number: int, title: str, severity: str) -> None:
        colour = self.style.ERROR if severity in ("CRITICAL", "HIGH") else self.style.WARNING
        self.stdout.write("")
        self.stdout.write(f"  {colour(f'BUG {number}')}  {title}   [{severity}]")
        self.stdout.write("  " + "─" * 72)

    def _line(self, label: str, value: str) -> None:
        self.stdout.write(f"      {label:.<22} {value}")

    def _verdict(self, fixed: bool, reason: str) -> bool:
        self.stdout.write("")
        if fixed:
            self.stdout.write(f"      {self.style.SUCCESS('PASS')}  defect not reproducible")
        else:
            self.stdout.write(f"      {self.style.ERROR('FAIL')}  {reason}")
        return fixed
