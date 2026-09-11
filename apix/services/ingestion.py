"""Collection orchestration and immutable ingestion.

This service turns a :class:`~apix.models.CollectionRun` into work: it expands
the (source x route x lead window) cross product, drives each collector through
the :class:`~apix.policies.engine.PolicyEngine`, and writes every response into
the write-once :class:`~apix.models.RawObservation` table.

Two properties matter more than throughput:

*Idempotence.*  ``ingress_hash`` is unique, so re-running a collection that
partially failed inserts only what is genuinely new.  A source that returns a
byte-identical payload twice is stored once and the duplicate is counted.

*Fail-safe halting.*  A bot challenge aborts the whole source immediately - not
just the task - because continuing to probe a host that is actively challenging
us is the exact behaviour the compliance policy forbids.  The run is marked
``BLOCKED`` for that source and an operator is paged through the audit log.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apix.collectors.base import BaseCollector, CollectionTask, CollectorOutcome
from apix.collectors.registry import SourceRegistry, UnknownSourceError
from apix.enums import AuditAction, CollectionStatus, PayloadKind
from apix.models import AuditLog, CollectionRun, RawObservation, Route, Source
from apix.policies.engine import PolicyEngine, get_policy_engine

__all__ = ["CollectionService", "IngestionResult", "RunPlan", "build_plan"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunPlan:
    """The full cross product a run will attempt."""

    sources: tuple[Source, ...]
    routes: tuple[Route, ...]
    lead_windows: tuple[int, ...]
    observation_date: date

    @property
    def task_count(self) -> int:
        return len(self.sources) * len(self.routes) * len(self.lead_windows)

    def tasks_for(self, source: Source, *, run: CollectionRun | None = None) -> list[CollectionTask]:
        tasks: list[CollectionTask] = []
        for route in self.routes:
            for window in self.lead_windows:
                tasks.append(
                    CollectionTask(
                        route_code=route.code,
                        origin=route.origin,
                        destination=route.destination,
                        departure_date=self.observation_date + timedelta(days=window),
                        lead_window_days=window,
                        observation_date=self.observation_date,
                        currency=settings.APIX["CURRENCY"],
                        max_results=int(settings.APIX.get("MAX_QUOTES_PER_TASK", 300)),
                        run_id=run.id if run else None,
                        correlation_id=run.correlation_id if run else "",
                    )
                )
        return tasks


@dataclass
class IngestionResult:
    raw_created: int = 0
    raw_duplicate: int = 0
    tasks_attempted: int = 0
    tasks_failed: int = 0
    tasks_blocked: int = 0
    sources_halted: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    raw_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_created": self.raw_created,
            "raw_duplicate": self.raw_duplicate,
            "tasks_attempted": self.tasks_attempted,
            "tasks_failed": self.tasks_failed,
            "tasks_blocked": self.tasks_blocked,
            "sources_halted": self.sources_halted,
            "errors": self.errors[:100],
            "error_total": len(self.errors),
        }


def build_plan(
    *,
    observation_date: date | None = None,
    route_codes: Sequence[str] | None = None,
    lead_windows: Sequence[int] | None = None,
    source_codes: Sequence[str] | None = None,
) -> RunPlan:
    """Resolve a plan from the reference tables, honouring compliance state."""
    cfg = settings.APIX
    routes_qs = Route.objects.in_basket() if not route_codes else Route.objects.active().filter(
        code__in=[code.upper() for code in route_codes]
    )
    sources_qs = Source.objects.collectable()
    if source_codes:
        sources_qs = sources_qs.filter(code__in=source_codes)
    if not settings.APIX.get("PREFER_MOCK_SOURCES", False):
        sources_qs = sources_qs.exclude(kind="MOCK_FEED") if settings.APIX.get(
            "ALLOW_MOCK_IN_INDEX", True
        ) is False else sources_qs

    return RunPlan(
        sources=tuple(sources_qs.select_related("policy")),
        routes=tuple(routes_qs),
        lead_windows=tuple(lead_windows or cfg["LEAD_WINDOWS"]),
        observation_date=observation_date or timezone.localtime().date(),
    )


class CollectionService:
    """Drives one collection run from plan to persisted raw payloads."""

    def __init__(self, *, policy: PolicyEngine | None = None) -> None:
        self.policy = policy or get_policy_engine()

    # -- entry point -------------------------------------------------------- #
    def execute(self, run: CollectionRun, plan: RunPlan) -> IngestionResult:
        result = IngestionResult()
        run.mark(CollectionStatus.RUNNING)
        AuditLog.record(
            AuditAction.COLLECTION_TRIGGERED,
            summary=f"run {run.id}: {plan.task_count} tasks across {len(plan.sources)} sources",
            entity=run,
            context={
                "routes": [route.code for route in plan.routes],
                "lead_windows": list(plan.lead_windows),
                "sources": [source.code for source in plan.sources],
            },
            correlation_id=run.correlation_id,
        )

        for source in plan.sources:
            self._collect_source(source, plan, run, result)

        status = self._final_status(result)
        run.raw_count = result.raw_created
        run.stats = {**(run.stats or {}), "ingestion": result.as_dict()}
        run.save(update_fields=["raw_count", "stats"])
        run.mark(status)
        AuditLog.record(
            AuditAction.COLLECTION_COMPLETED,
            summary=f"run {run.id}: {result.raw_created} raw observations",
            entity=run,
            context=result.as_dict(),
            correlation_id=run.correlation_id,
        )
        return result

    # -- per source --------------------------------------------------------- #
    def _collect_source(
        self, source: Source, plan: RunPlan, run: CollectionRun, result: IngestionResult
    ) -> None:
        try:
            collector = SourceRegistry.create_for_source(
                source, policy=self.policy, attributes=source.attributes
            )
        except UnknownSourceError as exc:
            result.errors.append({"source": source.code, "error": str(exc)})
            logger.error("no collector for source", extra={"source": source.code})
            return

        route_by_code = {route.code: route for route in plan.routes}

        with collector:
            for task in plan.tasks_for(source, run=run):
                result.tasks_attempted += 1
                outcome = collector.run(task)

                if outcome.halted:
                    # Terminal for this source: stop the loop, do not retry, do
                    # not move to the next route "to see if it works there".
                    result.tasks_blocked += 1
                    result.sources_halted.append(source.code)
                    result.errors.append(
                        {"source": source.code, "route": task.route_code,
                         "error": outcome.error, "halted": True}
                    )
                    logger.error(
                        "source halted mid-run; abandoning remaining tasks",
                        extra={"source": source.code, "remaining": "aborted"},
                    )
                    return

                if outcome.blocked_by_policy:
                    result.tasks_blocked += 1
                    result.errors.append(
                        {"source": source.code, "route": task.route_code,
                         "error": outcome.error, "policy": True}
                    )
                    continue

                if not outcome.ok or outcome.response is None:
                    result.tasks_failed += 1
                    result.errors.append(
                        {"source": source.code, "route": task.route_code,
                         "error": outcome.error, "kind": outcome.error_kind}
                    )
                    continue

                self._persist(outcome, source, route_by_code[task.route_code], run, result)

    # -- persistence -------------------------------------------------------- #
    @staticmethod
    def _persist(
        outcome: CollectorOutcome,
        source: Source,
        route: Route,
        run: CollectionRun,
        result: IngestionResult,
    ) -> None:
        """Write the payload once.  A repeated ingress hash is a no-op."""
        response = outcome.response
        assert response is not None
        digest = response.ingress_hash

        try:
            with transaction.atomic():
                raw = RawObservation.objects.create(
                    ingress_hash=digest,
                    source=source,
                    collection_run=run,
                    route=route,
                    lead_window_days=outcome.task.lead_window_days,
                    search_departure_date=outcome.task.departure_date,
                    payload_kind=response.payload_kind or PayloadKind.JSON,
                    payload=response.payload,
                    request_url=response.request_url[:1_000],
                    request_method=response.request_method,
                    http_status=response.http_status,
                    response_headers=dict(response.response_headers or {}),
                    content_bytes=response.content_bytes,
                    captured_at=response.captured_at,
                    collector=response.collector or source.code,
                )
            result.raw_created += 1
            result.raw_ids.append(raw.pk)
        except IntegrityError:
            # Same bytes already captured - genuinely idempotent, not an error.
            result.raw_duplicate += 1
            logger.debug("duplicate ingress hash", extra={"source": source.code, "hash": digest[:12]})

    @staticmethod
    def _final_status(result: IngestionResult) -> str:
        if result.sources_halted:
            return CollectionStatus.BLOCKED
        if result.tasks_failed and not (result.raw_created or result.raw_duplicate):
            return CollectionStatus.FAILED
        if result.tasks_failed or result.tasks_blocked:
            return CollectionStatus.PARTIAL
        if result.raw_created == 0 and result.raw_duplicate == 0 and result.tasks_attempted:
            return CollectionStatus.FAILED
        return CollectionStatus.SUCCEEDED


def collector_for(source_code: str) -> BaseCollector:
    """Convenience accessor used by management commands and the admin."""
    source = Source.objects.get(code=source_code)
    return SourceRegistry.create_for_source(
        source, policy=get_policy_engine(), attributes=source.attributes
    )


def iter_unprocessed(limit: int = 1_000) -> Iterable[RawObservation]:
    return RawObservation.objects.unprocessed().select_related("source", "route")[:limit]