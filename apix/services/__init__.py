"""Domain services: ingestion, cleaning, statistics, provenance."""

from __future__ import annotations

from apix.services.cleaning import CleaningReport, FareCleaningPipeline, clean_raw_observations
from apix.services.currency import MoneyParseResult, ParseStatus, parse_money
from apix.services.indexer import (
    IndexCompiler,
    JevonsCalculator,
    LaspeyresAggregator,
    MatchedPair,
    compile_index_for_date,
)
from apix.services.ingestion import CollectionService, RunPlan, build_plan
from apix.services.outliers import MADOutlierDetector, OutlierReport
from apix.services.provenance import ProvenanceSealer, merkle_root, sha256_hex, verify_series

__all__ = [
    "CleaningReport",
    "CollectionService",
    "FareCleaningPipeline",
    "IndexCompiler",
    "JevonsCalculator",
    "LaspeyresAggregator",
    "MADOutlierDetector",
    "MatchedPair",
    "MoneyParseResult",
    "OutlierReport",
    "ParseStatus",
    "ProvenanceSealer",
    "RunPlan",
    "build_plan",
    "clean_raw_observations",
    "compile_index_for_date",
    "merkle_root",
    "parse_money",
    "sha256_hex",
    "verify_series",
]
