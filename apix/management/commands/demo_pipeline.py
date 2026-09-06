"""Run the whole APIx pipeline in memory and narrate every step.

    python manage.py demo_pipeline

No PostgreSQL, no Redis, no Docker, no network.  It exercises the real
production code paths - the same collector, the same parser, the same auditor,
the same Jevons and Laspeyres calculators, the same provenance sealer - and
prints what each one did, so the arithmetic is visible rather than implied.

The only thing it skips is persistence (`_persist`), which needs a database.
Everything printed below is computed live.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apix.collectors.base import CollectionTask
from apix.collectors.registry import SourceRegistry
from apix.enums import InventoryStatus, QualityGrade
from apix.models import Route, Source
from apix.services.cleaning import FareCleaningPipeline, StagedFare
from apix.services.currency import parse_money
from apix.services.indexer import JevonsCalculator, LaspeyresAggregator, MatchedPair
from apix.services.provenance import ProvenanceSealer

UTC = timezone.utc

#: The DGCA basket, with illustrative passenger volumes (see the paper, §13.3).
BASKET: tuple[tuple[str, str, str, int], ...] = (
    ("DEL-BOM", "DEL", "BOM", 5_100_000),
    ("DEL-BLR", "DEL", "BLR", 3_900_000),
    ("BOM-BLR", "BOM", "BLR", 2_800_000),
    ("DEL-CCU", "DEL", "CCU", 2_100_000),
    ("BLR-HYD", "BLR", "HYD", 1_600_000),
    ("MAA-DEL", "MAA", "DEL", 1_900_000),
)

WINDOWS = (1, 7, 15, 30, 45)


class Command(BaseCommand):
    help = "Run the full collection → cleaning → index → provenance pipeline in memory."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--window", type=int, default=7, choices=list(WINDOWS),
                            help="Booking window to compile the index for (default 7).")
        parser.add_argument("--base-date", default="2025-04-15",
                            help="Base-period observation date (default 2025-04-15).")
        parser.add_argument("--current-date", default="2026-09-01",
                            help="Current observation date (default 2026-09-01).")
        parser.add_argument("--show-payload", action="store_true",
                            help="Print one raw payload row verbatim.")

    # ------------------------------------------------------------------ #
    def handle(self, *args: Any, **options: Any) -> None:
        # Windows consoles default to cp1252 and choke on the rupee sign.
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - older interpreters
            pass

        window: int = options["window"]
        base_date = date.fromisoformat(options["base_date"])
        current_date = date.fromisoformat(options["current_date"])

        self._banner("APIx  ·  end-to-end pipeline demonstration")
        self._kv("booking window", f"T+{window}")
        self._kv("base period observation", base_date.isoformat())
        self._kv("current observation", current_date.isoformat())
        self._kv("routes", f"{len(BASKET)} (DGCA basket)")

        SourceRegistry.autodiscover()
        collector = SourceRegistry.create("mock_feed")
        source = Source(code="mock_feed", trust_rank=90, base_url="https://mock.apix.local")

        # ---------------------------------------------------------------- #
        self._step(1, "COLLECT — the synthetic carrier feed")
        sample_task = self._task(BASKET[0], window, current_date)
        response = collector.fetch(sample_task).sized()
        self._kv("route", sample_task.route_code)
        self._kv("rows returned", str(len(response.payload["results"])))
        self._kv("payload bytes", f"{response.content_bytes:,}")
        self._kv("ingress hash", response.ingress_hash)

        repeat = SourceRegistry.create("mock_feed").fetch(sample_task).sized()
        same = repeat.ingress_hash == response.ingress_hash
        self._kv("re-fetch hash identical", "YES  →  ingestion is idempotent" if same else "NO")

        if options["show_payload"]:
            self._sub("one row, verbatim as the source served it")
            row = response.payload["results"][0]
            for key, value in row.items():
                self.stdout.write(f"      {key:<18} {value}")

        # ---------------------------------------------------------------- #
        self._step(2, "PARSE — localized currency strings → exact Decimal")
        for raw in ("₹ 4,521.00", "INR 4521", "Rs. 4,521", "₹4.521,00",
                    "1,23,456.78", "1.2k", "3.4 lakh", "0", "--", "Sold out", None):
            result = parse_money(raw)
            shown = "None" if result.value is None else str(result.value)
            marker = "   ← unknown, NOT zero" if result.value is None else (
                "   ← genuine zero" if result.value == 0 else "")
            self.stdout.write(f"      {str(raw)!r:<16} → {shown:<12} [{result.status}]{marker}")

        # ---------------------------------------------------------------- #
        self._step(3, "CLEAN — seven stages over the whole basket")
        pipeline = FareCleaningPipeline()
        current_fares = self._clean(pipeline, collector, source, window, current_date)
        counters = pipeline.report.counters

        self._kv("rows extracted", f"{counters.extracted:,}")
        self._kv("survived cleaning", f"{len(current_fares):,}")
        self._kv("sold out / unpriced", str(counters.sold_out))
        self._kv("partial breakdown", str(counters.partial_breakdown))
        self._kv("duplicates suppressed", str(counters.duplicates))
        self._kv("outliers flagged", str(counters.outliers))
        self._kv("structural rejects", str(counters.structural_rejects))

        # ---------------------------------------------------------------- #
        self._step(4, "INVARIANT — missing is not zero, unpriced is not free")
        sold_out = [f for f in current_fares if f.inventory_status == InventoryStatus.SOLD_OUT]
        partial = [f for f in current_fares if f.quality_grade == QualityGrade.B_TOTAL_ONLY]
        full = [f for f in current_fares if f.quality_grade == QualityGrade.A_FULL_BREAKDOWN]

        if sold_out:
            f = sold_out[0]
            self._sub("a SOLD_OUT observation (scarcity, retained)")
            self.stdout.write(f"      {f.route.code} {f.flight_number}   total_fare = "
                              f"{'NULL' if f.total_fare is None else f.total_fare}"
                              f"   ← not 0.00; ln(0) would destroy the stratum")
        if partial:
            f = partial[0]
            self._sub("an all-in price with no component breakdown")
            self.stdout.write(f"      {f.route.code} {f.flight_number}   total = {f.total_fare}   "
                              f"base = {'NULL' if f.base_fare is None else f.base_fare}"
                              f"   ← not 0.00; the base-fare sub-index stays honest")
        if full:
            f = full[0]
            self._sub("a fully disclosed fare (grade A, reconciles to total)")
            self.stdout.write(
                f"      base {f.base_fare} + UDF {f.udf} + PSF {f.psf} + ASF {f.asf} "
                f"+ GST {f.gst} + other {f.other_charges}"
            )
            total = sum(v for v in (f.base_fare, f.udf, f.psf, f.asf, f.gst, f.other_charges)
                        if v is not None)
            self.stdout.write(f"      components sum {total}   total_fare {f.total_fare}   "
                              f"residual {f.total_fare - total}")

        # ---------------------------------------------------------------- #
        self._step(5, "SCREEN — Modified Z-score over MAD")
        flagged = [f for f in current_fares if f.is_outlier]
        if flagged:
            self._sub(f"{len(flagged)} flagged, all retained in the table")
            for f in flagged[:5]:
                self.stdout.write(f"      {f.route.code} {f.flight_number:<8} ₹{f.total_fare:>10,}"
                                  f"   z = {f.modified_z_score}")
        else:
            self._sub("no observation exceeded |M| > 3.5 in this sample")
        scored = [f for f in current_fares if f.modified_z_score is not None]
        self._kv("observations scored", str(len(scored)))
        self._kv("excluded from the aggregate", str(len(flagged)))

        # ---------------------------------------------------------------- #
        self._step(6, "COMPILE — Jevons elementary index, in log space")
        base_pipeline = FareCleaningPipeline()
        base_fares = self._clean(base_pipeline, collector, source, window, base_date)

        jevons = JevonsCalculator(base_level=Decimal("100"), trim_fraction=Decimal("0.02"))
        base_prices = self._item_prices(base_fares, jevons)
        current_prices = self._item_prices(current_fares, jevons)

        self.stdout.write("")
        self.stdout.write("      route      pairs   base ₹      current ₹    index")
        self.stdout.write("      " + "─" * 54)

        elementary: dict[str, Any] = {}
        for code, *_ in BASKET:
            pairs = [
                MatchedPair(item, base_prices[code][item], current_prices[code][item])
                for item in sorted(current_prices[code])
                if item in base_prices[code]
            ]
            result = jevons.elementary_index(pairs)
            elementary[code] = result
            if pairs:
                mean_base = sum(p.base_price for p in pairs) / len(pairs)
                mean_now = sum(p.current_price for p in pairs) / len(pairs)
                self.stdout.write(
                    f"      {code:<10} {result.pairs_used:>4}   {mean_base:>9,.0f}   "
                    f"{mean_now:>10,.0f}   {result.index_value:>9.4f}"
                )
        self.stdout.write("")
        self.stdout.write("      I = 100 · exp( (1/n) · Σ ln(p_t / p_0) )   ← never a running product")

        # ---------------------------------------------------------------- #
        self._step(7, "AGGREGATE — DGCA-weighted Laspeyres")
        total_pax = Decimal(sum(pax for *_r, pax in BASKET))
        shares = {code: (Decimal(pax) / total_pax).quantize(Decimal("0.0000000001"))
                  for code, _o, _d, pax in BASKET}

        aggregate = LaspeyresAggregator().aggregate(elementary, shares)

        self.stdout.write("")
        self.stdout.write("      route        DGCA share   index      weight     contribution")
        self.stdout.write("      " + "─" * 62)
        for row in aggregate.contributions:
            if row.included:
                self.stdout.write(
                    f"      {row.route_code:<12} {row.raw_share:>10.6f}   "
                    f"{row.index_value:>8.4f}   {row.effective_weight:>8.6f}   {row.contribution:>10.4f}"
                )
            else:
                self.stdout.write(f"      {row.route_code:<12} {row.raw_share:>10.6f}   "
                                  f"excluded — {row.exclusion_reason}")
        self.stdout.write("      " + "─" * 62)
        self.stdout.write(f"      NATIONAL COMPOSITE  T+{window}                        "
                          f"{aggregate.index_value:>10.4f}")
        self.stdout.write("")
        self._kv("weight coverage", f"{aggregate.weight_coverage:.4%}")
        self._kv("routes included", f"{aggregate.included_routes} of {len(BASKET)}")
        publishable = aggregate.weight_coverage >= Decimal("0.60")
        self._kv("publishable", "YES" if publishable else "NO — below the 60% coverage floor")

        # ---------------------------------------------------------------- #
        self._step(8, "SEAL — SHA-256 provenance")
        ingress = sorted({f"{i:064x}" for i in range(len(current_fares))})
        sealer = ProvenanceSealer(methodology_code="APIX_JEVONS_V1")
        common = dict(scope="NATIONAL", lead_window_days=window, base_period="2025-04",
                      ingress_hashes=ingress, computed_at=datetime(2026, 9, 1, 5, 30, tzinfo=UTC))

        day1 = sealer.seal(index_date=current_date, index_value=aggregate.index_value, **common)
        day2 = sealer.seal(index_date=current_date + timedelta(days=1),
                           index_value=aggregate.index_value + Decimal("0.42"),
                           previous_hash=day1.provenance_hash, **common)

        self._kv("inputs (Merkle root)", day1.inputs_root)
        self._kv("raw observations sealed", f"{day1.raw_observation_count:,}")
        self._kv("day 1 provenance hash", day1.provenance_hash)
        self._kv("day 2 previous_hash", day2.previous_hash)
        self._kv("chain intact", "YES" if day2.previous_hash == day1.provenance_hash else "NO")

        tampered = sealer.seal(index_date=current_date,
                               index_value=aggregate.index_value + Decimal("0.01"), **common)
        self.stdout.write("")
        self.stdout.write("      Tamper test — restate day 1 by one paisa:")
        self.stdout.write(f"        original : {day1.provenance_hash[:48]}…")
        self.stdout.write(f"        restated : {tampered.provenance_hash[:48]}…")
        self.stdout.write("        → day 2's back-link no longer matches; every later value fails")

        # merkle_root() itself is an ORDERED tree - reversing its input changes
        # the root, as it must.  Order independence is a guarantee of the SEALER,
        # which sorts and deduplicates before folding, so collection order cannot
        # change a published root.
        shuffled = list(reversed(ingress))
        reordered = sealer.seal(index_date=current_date, index_value=aggregate.index_value,
                                **{**common, "ingress_hashes": shuffled})
        self.stdout.write("")
        self.stdout.write("      Collection order cannot change the sealed root:")
        self.stdout.write(f"        as collected : {day1.inputs_root[:44]}…")
        self.stdout.write(f"        reversed     : {reordered.inputs_root[:44]}…")
        self.stdout.write(f"        → {'identical' if reordered.inputs_root == day1.inputs_root else 'DIFFERENT'}"
                          f"   (the sealer sorts; merkle_root itself is an ordered tree)")

        # ---------------------------------------------------------------- #
        self._banner("done")
        self.stdout.write(self.style.SUCCESS(
            f"  National APIx (T+{window}, {current_date}) = {aggregate.index_value:.4f}   "
            f"(base {common['base_period']} = 100)"
        ))
        self.stdout.write("")
        self.stdout.write("  Nothing here touched a database. Every number was computed by the")
        self.stdout.write("  same code the production workers run.")
        self.stdout.write("")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _task(entry: tuple[str, str, str, int], window: int, observation: date) -> CollectionTask:
        code, origin, destination, _pax = entry
        return CollectionTask(
            route_code=code, origin=origin, destination=destination,
            departure_date=observation + timedelta(days=window),
            lead_window_days=window, observation_date=observation,
        )

    def _clean(self, pipeline: FareCleaningPipeline, collector: Any, source: Source,
               window: int, observation: date) -> list[StagedFare]:
        """Run stages 2-7 over the whole basket, in memory."""
        staged: list[StagedFare] = []
        for entry in BASKET:
            code, origin, destination, _pax = entry
            route = Route(origin=origin, destination=destination, code=code)
            task = self._task(entry, window, observation)
            payload = collector.fetch(task).payload
            observed_at = datetime.combine(observation, datetime.min.time(), tzinfo=UTC)

            rows = collector.extract(payload, task)
            pipeline.report.counters.extracted += len(rows)

            for row in rows:
                fare = StagedFare(
                    raw_observation_id=0, source=source, route=route,
                    lead_window_days=window, observation_date=observation,
                    observed_at=observed_at,
                )
                if not pipeline._stage_2_currency(dict(row), fare):
                    continue
                if not pipeline._stage_3_canonicalise(dict(row), fare):
                    continue
                if not pipeline._stage_4_validate(fare):
                    continue
                pipeline._stage_5_audit_components(fare)
                fare.fingerprint = pipeline._fingerprint(fare)
                fare.content_hash = pipeline._content_hash(fare)
                staged.append(fare)

        staged = pipeline._stage_6_deduplicate(staged)
        pipeline._stage_7_screen_outliers(staged)
        return staged

    @staticmethod
    def _item_prices(fares: list[StagedFare], jevons: JevonsCalculator) -> dict[str, dict[str, Decimal]]:
        """Index-eligible fares, geometrically averaged per matched item."""
        buckets: dict[str, dict[str, list[Decimal]]] = {}
        for fare in fares:
            if fare.is_duplicate or fare.is_outlier or not fare.total_fare:
                continue
            if fare.quality_grade not in QualityGrade.index_eligible():
                continue
            item = f"{fare.carrier_iata}|{fare.flight_number}|{fare.cabin}"
            buckets.setdefault(fare.route.code, {}).setdefault(item, []).append(fare.total_fare)

        return {
            code: {item: jevons.geometric_mean(values).quantize(Decimal("0.01"))
                   for item, values in items.items()}
            for code, items in buckets.items()
        }

    # -- presentation --------------------------------------------------- #
    def _banner(self, text: str) -> None:
        self.stdout.write("")
        self.stdout.write("═" * 72)
        self.stdout.write(f"  {text.upper()}")
        self.stdout.write("═" * 72)

    def _step(self, number: int, title: str) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"  STEP {number} — {title}"))
        self.stdout.write("  " + "─" * 70)

    def _sub(self, text: str) -> None:
        self.stdout.write(f"    · {text}")

    def _kv(self, key: str, value: str) -> None:
        self.stdout.write(f"      {key:.<32} {value}")
