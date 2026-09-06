"""Tests for the statistical core.

These assert the properties a national index must have, not merely that the code
runs: exactness under Decimal, the Jevons/Laspeyres identities, the
"Missing != Zero" rule, and the tamper-evidence of the provenance chain.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from apix.services.currency import ParseStatus, parse_money, resolve_separators
from apix.services.indexer import JevonsCalculator, LaspeyresAggregator, MatchedPair
from apix.services.outliers import MADOutlierDetector, OutlierConfig, OutlierMethod, mad, median
from apix.services.provenance import ProvenanceSealer, canonical_json, merkle_root, sha256_hex

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Currency parsing
# --------------------------------------------------------------------------- #
class TestCurrencyParser:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("₹ 4,521.00", Decimal("4521.00")),
            ("INR 4521", Decimal("4521.00")),
            ("Rs. 4,521", Decimal("4521.00")),
            ("4,521.00", Decimal("4521.00")),
            ("₹4.521,00", Decimal("4521.00")),      # EU separators
            ("12 345.50", Decimal("12345.50")),     # thin-space grouping
            ("1,23,456.78", Decimal("123456.78")),  # Indian grouping
            ("1.2k", Decimal("1200.00")),
            ("3.4 lakh", Decimal("340000.00")),
            ("0", Decimal("0.00")),                 # explicit, genuine zero
        ],
    )
    def test_parses_localized_formats(self, raw: str, expected: Decimal) -> None:
        result = parse_money(raw)
        assert result.status is ParseStatus.OK
        assert result.value == expected

    @pytest.mark.parametrize("raw", ["", "--", "N/A", "Sold out", "price on request", None])
    def test_missing_never_becomes_zero(self, raw: object) -> None:
        """The whole platform depends on this distinction."""
        result = parse_money(raw)
        assert result.status is ParseStatus.MISSING
        assert result.value is None
        assert result.value != Decimal("0")

    def test_explicit_zero_is_not_missing(self) -> None:
        result = parse_money("0.00")
        assert result.is_ok and result.is_explicit_zero

    def test_negative_is_rejected_not_clamped(self) -> None:
        assert parse_money("-500").status is ParseStatus.NEGATIVE_REJECTED

    def test_foreign_currency_is_flagged_not_assumed(self) -> None:
        assert parse_money("$120", expected_currency="INR").status is ParseStatus.WRONG_CURRENCY

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("4,500", "4500"), ("4.5", "4.5")],
    )
    def test_separator_resolution(self, raw: str, expected: str) -> None:
        assert resolve_separators(raw) == expected

    def test_no_float_ever_appears(self) -> None:
        assert isinstance(parse_money(4521.35).value, Decimal)
        # 4521.35 is not representable in binary; via str() it round-trips exactly.
        assert parse_money(4521.35).value == Decimal("4521.35")


# --------------------------------------------------------------------------- #
# Outlier screening
# --------------------------------------------------------------------------- #
class TestModifiedZScore:
    def test_median_and_mad_are_exact(self) -> None:
        sample = [Decimal(v) for v in ("10", "12", "14", "16", "18")]
        assert median(sample) == Decimal("14")
        assert mad(sample) == Decimal("2")

    def test_flags_the_surge_without_deleting_it(self) -> None:
        base = [Decimal("5000")] * 9 + [Decimal("5100"), Decimal("4900"), Decimal("5200")]
        sample = [*base, Decimal("48000")]
        report = MADOutlierDetector(OutlierConfig(log_space=False)).detect(sample)

        assert report.outlier_count == 1
        assert report.outliers[0].value == Decimal("48000")
        # Every observation is still present in the report.
        assert len(report.verdicts) == len(sample)

    def test_falls_back_when_mad_is_zero(self) -> None:
        sample = [Decimal("5000")] * 9 + [Decimal("9000")]
        report = MADOutlierDetector(OutlierConfig(log_space=False)).detect(sample)
        assert report.method is OutlierMethod.MODIFIED_Z_MEANAD
        assert report.scale > 0

    def test_identical_sample_has_no_outliers(self) -> None:
        report = MADOutlierDetector().detect([Decimal("5000")] * 12)
        assert report.method is OutlierMethod.DEGENERATE_ZERO_SCALE
        assert report.outlier_count == 0

    def test_small_sample_is_left_alone(self) -> None:
        report = MADOutlierDetector().detect([Decimal("100"), Decimal("100000")])
        assert report.method is OutlierMethod.INSUFFICIENT_DATA
        assert report.outlier_count == 0

    def test_unpriced_rows_never_enter_the_sample(self) -> None:
        """A sold-out flight has no price; it cannot move the median."""
        records = [{"fare": Decimal("5000")} for _ in range(10)] + [{"fare": None}]
        reports, verdicts = MADOutlierDetector().screen_records(
            records, value_of=lambda r: r["fare"], stratum_of=lambda r: "DEL-BOM"
        )
        assert reports["DEL-BOM"].sample_size == 10


# --------------------------------------------------------------------------- #
# Jevons
# --------------------------------------------------------------------------- #
class TestJevons:
    def test_uniform_increase_reproduces_exactly(self) -> None:
        """Every price up 8% must give exactly 108, not 107.999..."""
        pairs = [
            MatchedPair(f"item{i}", Decimal("5000.00"), Decimal("5400.00")) for i in range(20)
        ]
        result = JevonsCalculator(trim_fraction=Decimal("0")).elementary_index(pairs)
        assert result.index_value == Decimal("108.00000000")
        assert result.pairs_used == 20

    def test_is_the_geometric_not_arithmetic_mean(self) -> None:
        """One doubling and one halving cancel under Jevons; a Carli would not."""
        pairs = [
            MatchedPair("up", Decimal("1000"), Decimal("2000")),
            MatchedPair("down", Decimal("1000"), Decimal("500")),
        ]
        result = JevonsCalculator(trim_fraction=Decimal("0"), min_pairs=1).elementary_index(pairs)
        assert result.index_value == Decimal("100.00000000")

    def test_log_space_survives_a_wide_sample(self) -> None:
        """A naive product of 400 relatives would overflow long before this."""
        pairs = [
            MatchedPair(f"item{i}", Decimal("1000"), Decimal(str(1000 * (1 + i % 50))))
            for i in range(400)
        ]
        result = JevonsCalculator(trim_fraction=Decimal("0")).elementary_index(pairs)
        assert result.index_value > 0
        assert result.pairs_used == 400

    def test_non_positive_prices_are_rejected_not_logged(self) -> None:
        pairs = [
            MatchedPair("ok", Decimal("1000"), Decimal("1100")),
            MatchedPair("zero", Decimal("1000"), Decimal("0")),
        ]
        result = JevonsCalculator(trim_fraction=Decimal("0"), min_pairs=1).elementary_index(pairs)
        assert result.pairs_rejected == 1
        assert result.index_value == Decimal("110.00000000")

    def test_trimming_removes_symmetric_tails(self) -> None:
        pairs = [MatchedPair(f"i{i}", Decimal("1000"), Decimal("1000")) for i in range(48)]
        pairs.append(MatchedPair("high", Decimal("1000"), Decimal("100000")))
        pairs.append(MatchedPair("low", Decimal("1000"), Decimal("10")))
        result = JevonsCalculator(trim_fraction=Decimal("0.02")).elementary_index(pairs)
        assert result.pairs_trimmed == 2
        assert result.index_value == Decimal("100.00000000")


# --------------------------------------------------------------------------- #
# Laspeyres
# --------------------------------------------------------------------------- #
class TestLaspeyres:
    @staticmethod
    def _elementary(value: str, pairs: int = 10):  # noqa: ANN205
        from apix.services.indexer import ElementaryResult

        return ElementaryResult(Decimal(value), Decimal("0"), pairs)

    def test_weighted_mean_of_route_indices(self) -> None:
        elementary = {"DEL-BOM": self._elementary("110"), "DEL-BLR": self._elementary("90")}
        shares = {"DEL-BOM": Decimal("0.75"), "DEL-BLR": Decimal("0.25")}
        result = LaspeyresAggregator().aggregate(elementary, shares)
        assert result.index_value == Decimal("105.00000000")
        assert result.weight_coverage == Decimal("1.0000000000")

    def test_missing_route_renormalises_and_reports_coverage(self) -> None:
        elementary = {"DEL-BOM": self._elementary("110")}
        shares = {"DEL-BOM": Decimal("0.60"), "DEL-BLR": Decimal("0.40")}
        result = LaspeyresAggregator().aggregate(elementary, shares)
        # The surviving route carries the whole index, and coverage says so.
        assert result.index_value == Decimal("110.00000000")
        assert result.weight_coverage == Decimal("0.6000000000")
        assert result.included_routes == 1

    def test_direct_form_agrees_with_the_weighted_mean(self) -> None:
        base = {"DEL-BOM": Decimal("5000"), "DEL-BLR": Decimal("6000")}
        current = {"DEL-BOM": Decimal("5500"), "DEL-BLR": Decimal("6000")}
        quantities = {"DEL-BOM": Decimal("1000"), "DEL-BLR": Decimal("1000")}
        direct = LaspeyresAggregator().laspeyres_from_prices(
            base_prices=base, current_prices=current, base_quantities=quantities
        )
        assert direct == Decimal("104.54545455")


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
class TestProvenance:
    def test_canonical_form_is_order_independent(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_decimal_scale_does_not_change_the_hash(self) -> None:
        assert sha256_hex({"v": Decimal("100")}) == sha256_hex({"v": Decimal("100.00")})

    def test_float_is_refused(self) -> None:
        with pytest.raises(TypeError, match="float is not permitted"):
            canonical_json({"v": 1.5})

    def test_merkle_root_is_deterministic_and_sensitive(self) -> None:
        digests = [sha256_hex({"i": i}) for i in range(5)]
        assert merkle_root(digests) == merkle_root(list(digests))
        assert merkle_root(digests) != merkle_root(digests[:-1])

    def test_seal_binds_inputs_and_predecessor(self) -> None:
        sealer = ProvenanceSealer(methodology_code="APIX_JEVONS_V1")
        moment = datetime(2026, 9, 1, 5, 30, tzinfo=UTC)
        common = {
            "index_date": date(2026, 9, 1),
            "scope": "NATIONAL",
            "lead_window_days": 7,
            "base_period": "2025-04",
            "computed_at": moment,
        }
        first = sealer.seal(index_value=Decimal("108"), ingress_hashes=["ab" * 32], **common)
        same = sealer.seal(index_value=Decimal("108"), ingress_hashes=["ab" * 32], **common)
        moved = sealer.seal(index_value=Decimal("108.01"), ingress_hashes=["ab" * 32], **common)
        rechained = sealer.seal(
            index_value=Decimal("108"), ingress_hashes=["ab" * 32], previous_hash="cd" * 32, **common
        )

        assert first.provenance_hash == same.provenance_hash          # reproducible
        assert first.provenance_hash != moved.provenance_hash          # value-sensitive
        assert first.provenance_hash != rechained.provenance_hash      # chain-sensitive

    def test_input_order_does_not_change_the_root(self) -> None:
        sealer = ProvenanceSealer(methodology_code="APIX_JEVONS_V1")
        kwargs = {
            "index_date": date(2026, 9, 1),
            "index_value": Decimal("108"),
            "scope": "NATIONAL",
            "lead_window_days": 7,
            "base_period": "2025-04",
            "computed_at": datetime(2026, 9, 1, tzinfo=UTC),
        }
        forward = sealer.seal(ingress_hashes=["aa" * 32, "bb" * 32], **kwargs)
        reverse = sealer.seal(ingress_hashes=["bb" * 32, "aa" * 32], **kwargs)
        assert forward.inputs_root == reverse.inputs_root
