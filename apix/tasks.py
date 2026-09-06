"""Celery tasks and the post-collection workflow.

Pipeline
--------
``collect_basket -> clean_collection_run -> compile_index -> verify_provenance``

The stages are separate tasks rather than one long job for three reasons:

* they have different failure modes and different retry policies - a flaky
  carrier endpoint should retry, a statistical compilation should not;
* they run on different queues with different concurrency (browser work is
  serialised per source, statistics is serialised globally because it appends to
  a hash chain);
* a failure mid-pipeline leaves the completed stages durable, so a re-run
  resumes from immutable raw payloads instead of re-hitting the sources.

Every task is idempotent.  ``RawObservation.ingress_hash`` is unique,
``CanonicalFare`` upserts on its natural key, and ``IndexValue`` is
``update_or_create``d on its series point.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from collections.abc import Sequence

from celery import chain, shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apix.enums import AuditAction, CircuitState, CollectionStatus
from apix.models import AuditLog, CanonicalFare, CollectionRun, IndexValue, RawObservation, Route, SourcePolicy
from apix.services.cleaning import FareCleaningPipeline
from apix.services.indexer import IndexCompiler
from apix.services.ingestion import CollectionService, build_plan
from apix.services.provenance import verify_series

__all__ = [
    "clean_collection_run",
    "collect_basket",
    "compile_daily_index",
    "dispatch_daily_basket",
    "run_full_pipeline",
    "sweep_compliance_state",
    "verify_provenance_chain",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Stage 1 - collection
# --------------------------------------------------------------------------- #
@shared_task(
    bind=True,
    name="apix.tasks.collect_basket",
    queue="collection",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def collect_basket(self: Any, run_id: str) -> dict[str, Any]:
    """Execute one :class:`~apix.models.CollectionRun`.

    Retries cover transport flakiness only.  A policy refusal or a bot challenge
    is **not** retried: those are decisions, not failures.
    """
    run = CollectionRun.objects.get(pk=run_id)
    run.celery_task_id = self.request.id or ""
    run.save(update_fields=["celery_task_id"])

    parameters = run.parameters or {}
    plan = build_plan(
        observation_date=date.fromisoformat(parameters["observation_date"])
        if parameters.get("observation_date") else None,
        route_codes=parameters.get("route_codes"),
        lead_windows=run.lead_windows or parameters.get("lead_windows"),
        source_codes=parameters.get("source_codes"),
    )

    try:
        result = CollectionService().execute(run, plan)
    except SoftTimeLimitExceeded:
        run.mark(CollectionStatus.FAILED, error="soft time limit exceeded")
        raise
    except Exception as exc:  # noqa: BLE001
        run.mark(CollectionStatus.FAILED, error=str(exc))
        logger.exception("collection run failed", extra={"run_id": str(run.id)})
        raise

    return {"run_id": str(run.id), "status": run.status, **result.as_dict()}


# --------------------------------------------------------------------------- #
# Stage 2 - cleaning
# --------------------------------------------------------------------------- #
@shared_task(name="apix.tasks.clean_collection_run", queue="cleaning", acks_late=True)
def clean_collection_run(previous: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Run the 7-stage pipeline over one run's raw payloads.

    Accepts the upstream task's return value so it can sit in a ``chain``.
    """
    resolved = run_id or (previous or {}).get("run_id")
    if not resolved:
        raise ValueError("clean_collection_run requires a run_id")

    run = CollectionRun.objects.get(pk=resolved)
    raw = list(
        RawObservation.objects.filter(collection_run=run, is_processed=False)
        .select_related("source", "route")
    )
    if not raw:
        logger.info("nothing to clean", extra={"run_id": resolved})
        return {"run_id": resolved, "cleaned": 0}

    report = FareCleaningPipeline().run(raw, collection_run=run)

    with transaction.atomic():
        run.canonical_count = report.counters.written
        run.duplicate_count = report.counters.duplicates
        run.outlier_count = report.counters.outliers
        run.rejected_count = len(report.rejections)
        run.stats = {**(run.stats or {}), "cleaning": report.as_dict()}
        run.save(update_fields=[
            "canonical_count", "duplicate_count", "outlier_count", "rejected_count", "stats",
        ])
        AuditLog.record(
            AuditAction.CLEANING_COMPLETED,
            summary=f"run {run.id}: {report.counters.written} canonical fares",
            entity=run,
            context=report.counters.as_dict(),
            correlation_id=run.correlation_id,
        )

    return {
        "run_id": resolved,
        "cleaned": report.counters.written,
        "duplicates": report.counters.duplicates,
        "outliers": report.counters.outliers,
        "observation_date": _run_observation_date(run).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Stage 3 - statistical compilation
# --------------------------------------------------------------------------- #
@shared_task(name="apix.tasks.compile_daily_index", queue="statistics", acks_late=True)
def compile_daily_index(
    previous: dict[str, Any] | None = None,
    index_date: str | None = None,
    lead_windows: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Compile Jevons route indices and the DGCA-weighted national composite.

    Run this queue with ``--concurrency=1``: compilation appends to a per-series
    hash chain, and two concurrent compilations of the same series would race on
    the chain head.
    """
    resolved = (
        date.fromisoformat(index_date) if index_date
        else date.fromisoformat((previous or {}).get("observation_date"))
        if (previous or {}).get("observation_date")
        else timezone.localtime().date()
    )
    windows = list(lead_windows or settings.APIX["LEAD_WINDOWS"])
    compiler = IndexCompiler()

    summary: dict[str, Any] = {"index_date": resolved.isoformat(), "windows": {}}
    for window in windows:
        outcome = compiler.compile(resolved, window)
        national = outcome.national_value
        summary["windows"][str(window)] = {
            "routes_published": len(outcome.route_values),
            "national_index": str(national.index_value) if national else None,
            "national_published": bool(national and national.is_published),
            "weight_coverage": str(national.weight_coverage) if national and national.weight_coverage else None,
            "suppression_reason": national.suppression_reason if national else "",
            "provenance_hash": national.provenance_hash if national else "",
        }
        logger.info(
            "index compiled",
            extra={
                "date": resolved.isoformat(),
                "window": window,
                "routes": len(outcome.route_values),
                "national": str(national.index_value) if national else None,
            },
        )
    return summary


# --------------------------------------------------------------------------- #
# Stage 4 - provenance verification
# --------------------------------------------------------------------------- #
@shared_task(name="apix.tasks.verify_provenance_chain", queue="statistics")
def verify_provenance_chain(
    previous: dict[str, Any] | None = None, lookback_days: int = 90
) -> dict[str, Any]:
    """Re-verify the published hash chains; a break is a P1 incident."""
    since = timezone.localtime().date() - timedelta(days=lookback_days)
    results: dict[str, Any] = {}

    scopes: list[tuple[Route | None, int | None]] = [
        (None, window) for window in settings.APIX["LEAD_WINDOWS"]
    ]
    scopes += [
        (route, window)
        for route in Route.objects.in_basket()
        for window in settings.APIX["LEAD_WINDOWS"]
    ]

    for route, window in scopes:
        values = list(
            IndexValue.objects.filter(
                route=route, lead_window_days=window, index_date__gte=since, is_published=True
            ).order_by("index_date")
        )
        if not values:
            continue
        verification = verify_series(values)
        key = f"{route.code if route else 'NATIONAL'}|T+{window}"
        results[key] = verification.as_dict()
        if not verification.intact:
            logger.error("PROVENANCE CHAIN BROKEN", extra={"scope": key, **verification.as_dict()})
            AuditLog.record(
                AuditAction.MANUAL_OVERRIDE,
                summary=f"provenance chain broken for {key}",
                context=verification.as_dict(),
            )
    return {"checked": len(results), "broken": [k for k, v in results.items() if not v["intact"]]}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@shared_task(name="apix.tasks.dispatch_daily_basket", queue="default")
def dispatch_daily_basket(
    route_codes: Sequence[str] | None = None,
    lead_windows: Sequence[int] | None = None,
    source_codes: Sequence[str] | None = None,
    triggered_by_id: int | None = None,
    reason: str = "scheduled",
) -> str:
    """Create a run and launch the full pipeline.  Returns the run id."""
    observation_date = timezone.localtime().date()
    run = CollectionRun.objects.create(
        status=CollectionStatus.PENDING,
        triggered_by_id=triggered_by_id,
        trigger_reason=reason,
        lead_windows=list(lead_windows or settings.APIX["LEAD_WINDOWS"]),
        parameters={
            "observation_date": observation_date.isoformat(),
            "route_codes": list(route_codes) if route_codes else None,
            "source_codes": list(source_codes) if source_codes else None,
            "lead_windows": list(lead_windows or settings.APIX["LEAD_WINDOWS"]),
        },
        scheduled_for=timezone.now(),
    )
    if route_codes:
        run.routes.set(Route.objects.filter(code__in=[code.upper() for code in route_codes]))
    else:
        run.routes.set(Route.objects.in_basket())

    run_full_pipeline(str(run.id))
    return str(run.id)


def run_full_pipeline(run_id: str) -> Any:
    """Queue collect -> clean -> compile -> verify for one run."""
    workflow = chain(
        collect_basket.s(run_id),
        clean_collection_run.s(),
        compile_daily_index.s(),
        verify_provenance_chain.s(),
    )
    return workflow.apply_async()


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
@shared_task(name="apix.tasks.sweep_compliance_state", queue="default")
def sweep_compliance_state() -> dict[str, Any]:
    """Move cooled-down breakers to HALF_OPEN and report halted sources.

    ``TRIPPED_PERMANENT`` is deliberately untouched: a bot challenge never
    self-heals, and this sweep must not become a back door that silently
    resumes collection from a host that told us to stop.
    """
    now = timezone.now()
    reopened: list[str] = []

    for policy in SourcePolicy.objects.filter(circuit_state=CircuitState.OPEN).select_related("source"):
        if policy.opened_at and now >= policy.opened_at + timedelta(seconds=policy.cooldown_seconds):
            SourcePolicy.objects.filter(pk=policy.pk).update(
                circuit_state=CircuitState.HALF_OPEN, consecutive_failures=0
            )
            reopened.append(policy.source.code)

    halted = list(
        SourcePolicy.objects.filter(
            circuit_state=CircuitState.TRIPPED_PERMANENT
        ).values_list("source__code", flat=True)
    )
    if halted:
        logger.warning("sources awaiting operator resume", extra={"sources": halted})
    return {"reopened": reopened, "halted": halted, "checked_at": now.isoformat()}


@shared_task(name="apix.tasks.clean_backlog", queue="cleaning")
def clean_backlog(limit: int = 2_000) -> dict[str, Any]:
    """Process any raw observations a failed run left behind."""
    raw = list(
        RawObservation.objects.unprocessed().select_related("source", "route")[:limit]
    )
    if not raw:
        return {"cleaned": 0}
    report = FareCleaningPipeline().run(raw)
    return {"cleaned": report.counters.written, "raw_processed": report.raw_processed}


@shared_task(name="apix.tasks.recompute_index_range", queue="statistics")
def recompute_index_range(start: str, end: str, lead_windows: Sequence[int] | None = None) -> dict[str, Any]:
    """Backfill or restate a date range.

    Restatement is an audited action: every recomputed value is re-sealed and
    the chain from that date forward must be re-verified, which
    :func:`verify_provenance_chain` will do on the next scheduled sweep.
    """
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ValueError("end must not precede start")
    compiler = IndexCompiler()
    windows = list(lead_windows or settings.APIX["LEAD_WINDOWS"])

    compiled = 0
    cursor = first
    while cursor <= last:
        for window in windows:
            compiler.compile(cursor, window)
            compiled += 1
        cursor += timedelta(days=1)

    AuditLog.record(
        AuditAction.MANUAL_OVERRIDE,
        summary=f"index recomputed for {start}..{end}",
        context={"windows": windows, "compilations": compiled},
    )
    return {"start": start, "end": end, "compilations": compiled}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_observation_date(run: CollectionRun) -> date:
    parameters = run.parameters or {}
    if parameters.get("observation_date"):
        return date.fromisoformat(parameters["observation_date"])
    first = (
        CanonicalFare.objects.filter(collection_run=run)
        .values_list("observation_date", flat=True)
        .first()
    )
    return first or timezone.localtime().date()
