"""Collector-tier tests: registry wiring, yield curve, and payload discipline.

None of these touch the database - the collectors are deliberately pure, which
is what makes a new carrier cheap to test.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from apix.collectors.base import BaseCollector, CollectionTask, RawCollectorResponse
from apix.collectors.mock import BOOKING_CURVE, BOOKING_CURVE_ANCHORS, MockCollector, yield_multiplier
from apix.collectors.registry import SourceRegistry, UnknownSourceError
from apix.enums import InventoryStatus


class TestSourceRegistry:
    def test_autodiscovery_finds_the_shipped_collectors(self) -> None:
        codes = SourceRegistry.autodiscover()
        assert "mock_feed" in codes
        assert "permitted_web_json" in codes

    def test_factory_returns_a_usable_collector(self) -> None:
        collector = SourceRegistry.create("mock_feed")
        assert isinstance(collector, MockCollector)
        assert collector.source_code == "mock_feed"

    def test_unknown_source_names_what_is_available(self) -> None:
        with pytest.raises(UnknownSourceError, match="mock_feed"):
            SourceRegistry.get("no_such_source")

    def test_registration_rejects_a_non_collector(self) -> None:
        with pytest.raises(Exception, match="not a BaseCollector"):
            SourceRegistry.register("bogus")(dict)  # type: ignore[arg-type]

    def test_entries_describe_browser_requirements(self) -> None:
        described = {row["source_code"]: row for row in SourceRegistry.describe_all()}
        assert described["mock_feed"]["requires_browser"] is False
        assert described["permitted_web_json"]["requires_browser"] is True


class TestBookingCurve:
    def test_covers_every_day_from_t1_to_t45(self) -> None:
        assert min(BOOKING_CURVE) == 1
        assert max(BOOKING_CURVE) == 45
        assert len(BOOKING_CURVE) == 45

    def test_published_windows_hit_their_anchors_exactly(self) -> None:
        """T+1, 7, 15, 30 and 45 are sampled windows - no interpolation drift."""
        for window, expected in BOOKING_CURVE_ANCHORS.items():
            assert yield_multiplier(window) == expected.quantize(Decimal("0.0001"))

    def test_is_monotonically_decreasing_in_lead_time(self) -> None:
        """Fares fall as the departure recedes - the defining yield property."""
        values = [BOOKING_CURVE[day] for day in sorted(BOOKING_CURVE)]
        assert all(earlier >= later for earlier, later in zip(values, values[1:], strict=False))

    def test_last_minute_surge_is_material(self) -> None:
        assert yield_multiplier(1) / yield_multiplier(45) > Decimal("2.5")

    def test_clamps_outside_the_modelled_band(self) -> None:
        assert yield_multiplier(0) == yield_multiplier(1)
        assert yield_multiplier(400) == yield_multiplier(45)


class TestMockCollector:
    @staticmethod
    def _task(window: int) -> CollectionTask:
        observation = date(2026, 9, 1)
        return CollectionTask(
            route_code="DEL-BOM", origin="DEL", destination="BOM",
            departure_date=observation + timedelta(days=window),
            lead_window_days=window, observation_date=observation,
        )

    def test_is_byte_for_byte_reproducible(self, mock_collector: MockCollector) -> None:
        """Two independent instances must hash identically or CI is useless."""
        first = MockCollector().fetch(self._task(7))
        second = MockCollector().fetch(self._task(7))
        assert first.ingress_hash == second.ingress_hash

    def test_different_windows_produce_different_prices(self) -> None:
        near = MockCollector().fetch(self._task(1))
        far = MockCollector().fetch(self._task(45))
        assert near.ingress_hash != far.ingress_hash

    def test_sold_out_rows_omit_every_price_key(self) -> None:
        """The pathology the pipeline must turn into NULL, never into 0."""
        collector = MockCollector(sold_out_rate=1.0)
        payload = collector.fetch(self._task(1)).payload
        rows = payload["results"]
        assert rows, "generator produced no rows"
        for row in rows:
            assert row["inventory_status"] == InventoryStatus.SOLD_OUT
            assert row["pricing"] == {}

    def test_partial_breakdown_omits_components_but_keeps_the_total(self) -> None:
        collector = MockCollector(sold_out_rate=0.0, partial_breakdown_rate=1.0)
        rows = collector.fetch(self._task(7)).payload["results"]
        for row in rows:
            assert set(row["pricing"]) == {"total_fare"}
            assert "base_fare" not in row["pricing"]

    def test_full_breakdown_publishes_every_statutory_component(self) -> None:
        collector = MockCollector(sold_out_rate=0.0, partial_breakdown_rate=0.0)
        rows = collector.fetch(self._task(7)).payload["results"]
        for row in rows:
            assert {"base_fare", "udf", "psf", "asf", "gst"} <= set(row["pricing"])

    def test_extract_preserves_key_absence(self) -> None:
        collector = MockCollector(sold_out_rate=1.0)
        task = self._task(1)
        fares = collector.extract(collector.fetch(task).payload, task)
        assert fares
        for fare in fares:
            assert "total_fare" not in fare
            assert fare["inventory_status"] == InventoryStatus.SOLD_OUT

    def test_run_without_a_policy_gate_still_succeeds(self) -> None:
        outcome = MockCollector().run(self._task(15))
        assert outcome.ok
        assert outcome.fare_count > 0
        assert outcome.response is not None
        assert outcome.response.content_bytes > 0

    def test_run_reports_a_policy_refusal_instead_of_raising(self) -> None:
        class RefusingGate:
            def authorize(self, **_: Any) -> None:
                error = RuntimeError("robots.txt disallows this path")
                error.policy_blocked = True  # type: ignore[attr-defined]
                raise error

            def observe_response(self, **_: Any) -> None: ...
            def observe_exception(self, **_: Any) -> None: ...

        outcome = MockCollector(policy=RefusingGate()).run(self._task(7))
        assert not outcome.ok
        assert outcome.blocked_by_policy
        assert "robots.txt" in outcome.error

    def test_run_halts_the_source_on_a_challenge_signal(self) -> None:
        from apix.policies.challenge import BotChallengeDetected, BotChallengeSignal

        class ChallengingGate:
            def authorize(self, **_: Any) -> None:
                raise BotChallengeDetected(
                    BotChallengeSignal("cloudflare", "body:just a moment", 0.97, 403),
                    source_code="mock_feed",
                )

            def observe_response(self, **_: Any) -> None: ...
            def observe_exception(self, **_: Any) -> None: ...

        outcome = MockCollector(policy=ChallengingGate()).run(self._task(7))
        assert outcome.halted and outcome.blocked_by_policy
        assert "no evasion" in outcome.error


class TestRawCollectorResponse:
    def test_hash_is_insensitive_to_key_order(self) -> None:
        first = RawCollectorResponse(payload={"a": 1, "b": 2}, request_url="https://x.test")
        second = RawCollectorResponse(payload={"b": 2, "a": 1}, request_url="https://x.test")
        assert first.ingress_hash == second.ingress_hash

    def test_hash_changes_with_content(self) -> None:
        first = RawCollectorResponse(payload={"fare": "100"}, request_url="https://x.test")
        second = RawCollectorResponse(payload={"fare": "101"}, request_url="https://x.test")
        assert first.ingress_hash != second.ingress_hash


class TestBaseCollectorContract:
    def test_concrete_subclass_must_declare_a_source_code(self) -> None:
        with pytest.raises(TypeError, match="source_code"):

            class Nameless(BaseCollector):
                def fetch(self, task: CollectionTask) -> RawCollectorResponse:  # noqa: D102
                    raise NotImplementedError

                def extract(self, payload: Any, task: CollectionTask) -> list[Any]:  # noqa: D102
                    return []

    def test_intermediate_base_classes_are_exempt(self) -> None:
        """PlaywrightCollector implements fetch but not extract - that is legal."""

        class HalfDone(BaseCollector):
            def fetch(self, task: CollectionTask) -> RawCollectorResponse:  # noqa: D102
                raise NotImplementedError

        assert HalfDone.source_code == ""

    def test_circular_route_is_rejected_at_task_construction(self) -> None:
        with pytest.raises(ValueError, match="circular route"):
            CollectionTask(
                route_code="DEL-DEL", origin="DEL", destination="DEL",
                departure_date=date(2026, 9, 8), lead_window_days=7,
                observation_date=date(2026, 9, 1),
            )
