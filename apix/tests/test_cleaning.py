from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest

from apix.enums import FareFlag, InventoryStatus, QualityGrade
from apix.models import Route, Source
from apix.services.cleaning import FareCleaningPipeline, StagedFare

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def pipeline():
    return FareCleaningPipeline()


@pytest.fixture
def sample_route():
    return Route(origin="DEL", destination="BOM", code="DEL-BOM")


@pytest.fixture
def sample_source():
    return Source(code="mock_feed", trust_rank=90)


@pytest.fixture
def base_staged_fare(sample_route, sample_source):
    return StagedFare(
        raw_observation_id=1,
        source=sample_source,
        route=sample_route,
        lead_window_days=7,
        observation_date=date(2026, 9, 1),
        observed_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
    )


@pytest.fixture
def base_payload():
    return {
        "carrier_iata": "6E",
        "flight_number": "6E2134",
        "cabin": "economy",
        "departure_local": "2026-09-08T06:00:00",
        "arrival_local": "2026-09-08T08:15:00",
        "currency": "INR",
        "seats_remaining": 5,
        "is_refundable": False,
        "base_fare": "3800.00",
        "taxes_fees": "721.00",
        "total_fare": "4521.00",
    }


def _stage_row(pipeline, payload, source, route, raw_id=1):
    fare = StagedFare(
        raw_observation_id=raw_id,
        source=source,
        route=route,
        lead_window_days=7,
        observation_date=date(2026, 9, 1),
        observed_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
    )
    if not pipeline._stage_2_currency(payload, fare):
        return None
    if not pipeline._stage_3_canonicalise(payload, fare):
        return None
    return fare


class TestPipelineDefectRegressions:
    def test_bug_1_fare_basis_prevents_fingerprint_collision(
        self, pipeline, base_payload, sample_source, sample_route
    ):
        saver_payload = {**base_payload, "fare_basis": "Q07SAVER", "booking_class": "Q", "total_fare": "4521.00"}
        flexi_payload = {**base_payload, "fare_basis": "M07FLEXI", "booking_class": "M", "total_fare": "7899.00"}

        saver = _stage_row(pipeline, saver_payload, sample_source, sample_route, raw_id=1)
        flexi = _stage_row(pipeline, flexi_payload, sample_source, sample_route, raw_id=2)

        assert saver is not None and flexi is not None
        assert pipeline._fingerprint(saver) != pipeline._fingerprint(flexi), (
            "Saver and Flexi fares on the same flight must produce distinct fingerprints."
        )

    def test_bug_3_zero_fare_converted_to_unpriced(self, pipeline, base_staged_fare, base_payload):
        payload = {**base_payload, "total_fare": "0"}
        assert pipeline._stage_2_currency(payload, base_staged_fare)
        assert pipeline._stage_3_canonicalise(payload, base_staged_fare)

        assert base_staged_fare.total_fare is None
        assert base_staged_fare.inventory_status == InventoryStatus.PRICE_UNAVAILABLE
        assert base_staged_fare.quality_grade == QualityGrade.D_UNUSABLE
        assert FareFlag.PRICE_UNAVAILABLE in base_staged_fare.flags

    def test_bug_4_sold_out_without_departure_time_retained_with_fallback(
        self, pipeline, base_staged_fare
    ):
        payload = {
            "carrier_iata": "6E",
            "flight_number": "6E2134",
            "cabin": "economy",
            "inventory_status": "SOLD_OUT",
        }
        assert pipeline._stage_2_currency(payload, base_staged_fare)
        assert pipeline._stage_3_canonicalise(payload, base_staged_fare)

        assert base_staged_fare.inventory_status == InventoryStatus.SOLD_OUT
        assert base_staged_fare.total_fare is None
        assert base_staged_fare.departure_utc is not None
        assert base_staged_fare.departure_local is not None
        assert FareFlag.MODELLED in base_staged_fare.flags

    def test_bug_5_corrupt_component_flagged_and_downgraded(self, pipeline, base_staged_fare, base_payload):
        payload = {**base_payload, "base_fare": "-500.00", "total_fare": "4521.00"}
        assert pipeline._stage_2_currency(payload, base_staged_fare)
        assert pipeline._stage_3_canonicalise(payload, base_staged_fare)
        pipeline._stage_5_audit_components(base_staged_fare)

        assert base_staged_fare.base_fare is None
        assert FareFlag.COMPONENT_UNPARSEABLE in base_staged_fare.flags
        assert base_staged_fare.quality_grade == QualityGrade.C_SUSPECT

    def test_bug_6_telemetry_counters_track_price_unavailable(
        self, pipeline, base_staged_fare, base_payload
    ):
        payload = {**base_payload, "total_fare": "0"}
        pipeline._stage_2_currency(payload, base_staged_fare)
        pipeline._stage_3_canonicalise(payload, base_staged_fare)

        assert pipeline.report.counters.price_unavailable == 1
        assert pipeline.report.counters.sold_out == 0
