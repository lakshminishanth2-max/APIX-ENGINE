"""Django admin - the operations console for the statistical team.

The admin is not decoration here: it is where a duty officer inspects a halted
source, audits a raw payload against a published index value, and decides
whether a flagged surge is a data fault or a real market event.  Every screen is
therefore built around those three questions, and every mutating action writes
an :class:`~apix.models.AuditLog` entry.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from apix.enums import (
    AuditAction,
    CircuitState,
    CollectionStatus,
    ComplianceStatus,
    FareFlag,
    InventoryStatus,
    QualityGrade,
)
from apix.models import (
    AuditLog,
    CanonicalFare,
    CollectionRun,
    IndexValue,
    RawObservation,
    Route,
    RouteWeight,
    Source,
    SourcePolicy,
)
from apix.policies.engine import get_policy_engine
from apix.services.provenance import verify_series
from apix.tasks import clean_backlog, dispatch_daily_basket

_STATUS_COLOURS = {
    ComplianceStatus.ACTIVE: "#15803d",
    ComplianceStatus.PAUSED: "#b45309",
    ComplianceStatus.HALTED_BOT_CHALLENGE: "#b91c1c",
    ComplianceStatus.HALTED_ROBOTS: "#b91c1c",
    ComplianceStatus.DISABLED: "#6b7280",
}

_GRADE_COLOURS = {
    QualityGrade.A_FULL_BREAKDOWN: "#15803d",
    QualityGrade.B_TOTAL_ONLY: "#0369a1",
    QualityGrade.C_SUSPECT: "#b45309",
    QualityGrade.D_UNUSABLE: "#b91c1c",
}


def _badge(text: str, colour: str) -> SafeString:
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;'
        'font-size:11px;font-weight:600;white-space:nowrap">{}</span>',
        colour,
        text,
    )


def _pretty_json(payload: Any, limit: int = 40_000) -> SafeString:
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)[:limit]
    return format_html(
        '<pre style="max-height:460px;overflow:auto;background:#0f172a;color:#e2e8f0;'
        'padding:12px;border-radius:6px;font-size:12px;line-height:1.5">{}</pre>',
        text,
    )


# --------------------------------------------------------------------------- #
# Sources and compliance
# --------------------------------------------------------------------------- #
class SourcePolicyInline(admin.StackedInline):
    model = SourcePolicy
    can_delete = False
    extra = 0
    readonly_fields = (
        "circuit_state", "consecutive_failures", "opened_at", "halted_at", "halt_reason",
        "last_success_at", "total_requests", "total_failures",
        "robots_last_checked", "robots_last_verdict", "live_bucket",
    )
    fieldsets = (
        ("Rate limiting", {"fields": ("requests_per_minute", "burst", "max_concurrency", "live_bucket")}),
        ("robots.txt", {"fields": ("respect_robots", "robots_url", "crawl_delay_seconds",
                                   "robots_last_checked", "robots_last_verdict", "user_agent")}),
        ("Circuit breaker", {"fields": ("compliance_status", "circuit_state", "failure_threshold",
                                        "cooldown_seconds", "consecutive_failures", "opened_at",
                                        "halted_at", "halt_reason")}),
        ("Counters", {"fields": ("total_requests", "total_failures", "last_success_at"),
                      "classes": ("collapse",)}),
    )

    @admin.display(description="Live token bucket")
    def live_bucket(self, obj: SourcePolicy) -> str:
        if not obj.pk:
            return "-"
        snapshot = get_policy_engine().snapshot(obj.source.code)["bucket"]
        return (
            f"{snapshot['tokens']:.2f} / {snapshot['capacity']:.0f} tokens "
            f"@ {snapshot['rate_per_second']:.3f}/s"
        )


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "carrier_iata", "trust_rank",
                    "is_active", "compliance_badge", "circuit_badge", "observation_count")
    list_filter = ("kind", "is_active", "policy__compliance_status", "policy__circuit_state")
    search_fields = ("code", "name", "carrier_iata", "base_url")
    ordering = ("trust_rank", "code")
    inlines = (SourcePolicyInline,)
    actions = ("action_pause", "action_resume", "action_force_resume")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("code", "name", "kind", "carrier_iata", "base_url", "contact_email")}),
        ("Statistical role", {"fields": ("trust_rank", "is_active"),
                              "description": "Trust rank orders cross-source deduplication; lower wins."}),
        ("Collector configuration", {"fields": ("attributes",),
                                     "description": "Passed verbatim to the collector - "
                                                    "search URL template, field map, response pattern."}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Compliance", ordering="policy__compliance_status")
    def compliance_badge(self, obj: Source) -> SafeString:
        policy = getattr(obj, "policy", None)
        if policy is None:
            return _badge("NO POLICY", "#b91c1c")
        return _badge(policy.compliance_status, _STATUS_COLOURS.get(policy.compliance_status, "#6b7280"))

    @admin.display(description="Breaker", ordering="policy__circuit_state")
    def circuit_badge(self, obj: Source) -> SafeString:
        policy = getattr(obj, "policy", None)
        if policy is None:
            return "-"
        colour = "#b91c1c" if policy.circuit_state == CircuitState.TRIPPED_PERMANENT else (
            "#b45309" if policy.circuit_state != CircuitState.CLOSED else "#15803d"
        )
        return _badge(policy.circuit_state, colour)

    @admin.display(description="Raw payloads")
    def observation_count(self, obj: Source) -> int:
        return obj.raw_observations.count()

    @admin.action(description="Pause selected sources (no collection)")
    def action_pause(self, request: HttpRequest, queryset: QuerySet[Source]) -> None:
        for source in queryset.select_related("policy"):
            policy = source.policy
            policy.compliance_status = ComplianceStatus.PAUSED
            policy.save(update_fields=["compliance_status", "updated_at"])
        self.message_user(request, f"Paused {queryset.count()} source(s).", messages.WARNING)

    @admin.action(description="Resume selected sources (soft)")
    def action_resume(self, request: HttpRequest, queryset: QuerySet[Source]) -> None:
        engine = get_policy_engine()
        resumed, refused = 0, []
        for source in queryset:
            if engine.resume(source.code, actor=request.user, note="admin bulk resume"):
                resumed += 1
            else:
                refused.append(source.code)
        if resumed:
            self.message_user(request, f"Resumed {resumed} source(s).", messages.SUCCESS)
        if refused:
            self.message_user(
                request,
                "Refused (halted by bot challenge - use the force action after confirming "
                f"access is permitted): {', '.join(refused)}",
                messages.ERROR,
            )

    @admin.action(description="FORCE resume halted sources (bot challenge - requires justification)")
    def action_force_resume(self, request: HttpRequest, queryset: QuerySet[Source]) -> None:
        engine = get_policy_engine()
        for source in queryset:
            engine.resume(source.code, actor=request.user, force=True, note="admin forced resume")
        self.message_user(
            request,
            f"Force-resumed {queryset.count()} source(s). Each action is recorded in the audit log. "
            "Confirm with the source that automated access is permitted.",
            messages.WARNING,
        )


@admin.register(SourcePolicy)
class SourcePolicyAdmin(admin.ModelAdmin):
    list_display = ("source", "compliance_status", "circuit_state", "requests_per_minute",
                    "burst", "consecutive_failures", "last_success_at", "halted_at")
    list_filter = ("compliance_status", "circuit_state", "respect_robots")
    search_fields = ("source__code", "halt_reason")
    readonly_fields = ("halted_at", "opened_at", "total_requests", "total_failures", "last_success_at")


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
class RouteWeightInline(admin.TabularInline):
    model = RouteWeight
    extra = 0
    ordering = ("-valid_from",)
    fields = ("valid_from", "valid_to", "passengers", "share", "reference_period",
              "source_document", "published_on")


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("code", "origin", "destination", "distance_km",
                    "is_in_basket", "is_active", "current_share", "fare_count")
    list_filter = ("is_in_basket", "is_active", "origin", "destination")
    search_fields = ("code", "origin", "destination", "origin_city", "destination_city")
    ordering = ("code",)
    inlines = (RouteWeightInline,)
    readonly_fields = ("code", "created_at", "updated_at")

    @admin.display(description="DGCA share (current)")
    def current_share(self, obj: Route) -> str:
        weight = obj.weights.filter(valid_to__isnull=True).order_by("-valid_from").first()
        return f"{weight.share:.6f}" if weight else "-"

    @admin.display(description="Canonical fares")
    def fare_count(self, obj: Route) -> int:
        return obj.canonical_fares.count()


@admin.register(RouteWeight)
class RouteWeightAdmin(admin.ModelAdmin):
    list_display = ("route", "reference_period", "valid_from", "valid_to", "passengers", "share")
    list_filter = ("reference_period", "route__code")
    search_fields = ("route__code", "source_document")
    date_hierarchy = "valid_from"

    def save_model(self, request: HttpRequest, obj: RouteWeight, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        AuditLog.record(
            AuditAction.WEIGHT_VERSION_ADDED,
            summary=f"{obj.route.code} {obj.reference_period}: share={obj.share}",
            entity=obj.route,
            actor=request.user,
            after={"share": str(obj.share), "valid_from": obj.valid_from.isoformat()},
        )


# --------------------------------------------------------------------------- #
# Immutable ingress
# --------------------------------------------------------------------------- #
@admin.register(RawObservation)
class RawObservationAdmin(admin.ModelAdmin):
    """Read-only by design: these rows are the evidence base."""

    list_display = ("short_hash", "source", "route", "lead_window_days", "http_status",
                    "content_bytes", "captured_at", "processed_badge")
    list_filter = ("source__code", "payload_kind", "is_processed", "lead_window_days")
    search_fields = ("ingress_hash", "request_url", "collector")
    date_hierarchy = "captured_at"
    ordering = ("-captured_at",)
    actions = ("action_reprocess",)
    readonly_fields = (
        "ingress_hash", "source", "collection_run", "route", "lead_window_days",
        "search_departure_date", "payload_kind", "request_url", "request_method",
        "http_status", "content_bytes", "captured_at", "collector",
        "is_processed", "processed_at", "processing_error",
        "payload_preview", "headers_preview", "derived_fares",
    )
    exclude = ("payload", "response_headers")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: RawObservation | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: RawObservation | None = None) -> bool:
        return False

    @admin.display(description="Ingress hash", ordering="ingress_hash")
    def short_hash(self, obj: RawObservation) -> str:
        return obj.ingress_hash[:16]

    @admin.display(description="Processed", boolean=True, ordering="is_processed")
    def processed_badge(self, obj: RawObservation) -> bool:
        return obj.is_processed

    @admin.display(description="Payload (verbatim)")
    def payload_preview(self, obj: RawObservation) -> SafeString:
        return _pretty_json(obj.payload)

    @admin.display(description="Response headers")
    def headers_preview(self, obj: RawObservation) -> SafeString:
        return _pretty_json(obj.response_headers)

    @admin.display(description="Derived canonical fares")
    def derived_fares(self, obj: RawObservation) -> SafeString:
        rows = obj.canonical_fares.all()[:50]
        if not rows:
            return mark_safe("<em>none</em>")
        return format_html_join(
            mark_safe("<br>"),
            '<a href="{}">{} {} T+{} = {}</a>',
            (
                (
                    reverse("admin:apix_canonicalfare_change", args=[row.pk]),
                    row.carrier_iata,
                    row.flight_number,
                    row.lead_window_days,
                    row.total_fare if row.total_fare is not None else "NULL (unpriced)",
                )
                for row in rows
            ),
        )

    @admin.action(description="Re-run the cleaning pipeline over selected payloads")
    def action_reprocess(self, request: HttpRequest, queryset: QuerySet[RawObservation]) -> None:
        queryset.update(is_processed=False, processing_error="")
        clean_backlog.delay(limit=queryset.count())
        self.message_user(
            request, f"Queued {queryset.count()} payload(s) for reprocessing.", messages.SUCCESS
        )


# --------------------------------------------------------------------------- #
# Canonical layer
# --------------------------------------------------------------------------- #
@admin.register(CanonicalFare)
class CanonicalFareAdmin(admin.ModelAdmin):
    list_display = ("observation_date", "route", "carrier_iata", "flight_number",
                    "lead_window_days", "fare_display", "grade_badge", "flag_badges", "source")
    list_filter = (
        "quality_grade", "is_outlier", "is_duplicate", "inventory_status",
        "lead_window_days", "cabin", "route__code", "source__code", "observation_date",
    )
    search_fields = ("carrier_iata", "flight_number", "fingerprint", "route__code")
    date_hierarchy = "observation_date"
    ordering = ("-observed_at",)
    list_select_related = ("route", "source")
    readonly_fields = (
        "fingerprint", "content_hash", "raw_observation_link", "duplicate_of",
        "modified_z_score", "outlier_stratum", "provenance_preview", "created_at",
    )
    fieldsets = (
        ("Flight", {"fields": ("route", "carrier_iata", "flight_number", "cabin",
                               "departure_utc", "departure_local", "arrival_utc")}),
        ("Sampling", {"fields": ("source", "raw_observation_link", "collection_run",
                                 "observed_at", "observation_date", "lead_window_days")}),
        ("Price components", {
            "fields": ("currency", "base_fare", "udf", "psf", "asf", "gst",
                       "other_charges", "taxes_fees", "total_fare"),
            "description": "NULL means the source did not publish the value. "
                           "It is never interchangeable with 0.00, which means genuinely waived.",
        }),
        ("Inventory and quality", {"fields": ("inventory_status", "seats_remaining", "is_refundable",
                                              "fare_basis", "booking_class", "quality_grade",
                                              "is_modeled", "flags")}),
        ("Statistical hygiene", {"fields": ("is_outlier", "modified_z_score", "outlier_stratum",
                                            "is_duplicate", "duplicate_of", "dedup_reason",
                                            "fingerprint", "content_hash")}),
        ("Lineage", {"fields": ("provenance_preview", "created_at"), "classes": ("collapse",)}),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.display(description="Total fare", ordering="total_fare")
    def fare_display(self, obj: CanonicalFare) -> SafeString:
        if obj.total_fare is None:
            reason = "SOLD OUT" if obj.inventory_status == InventoryStatus.SOLD_OUT else "UNPRICED"
            return _badge(f"NULL - {reason}", "#6b7280")
        colour = "#b45309" if obj.is_outlier else "#0f172a"
        return format_html('<b style="color:{}">{} {:,.2f}</b>', colour, obj.currency, obj.total_fare)

    @admin.display(description="Grade", ordering="quality_grade")
    def grade_badge(self, obj: CanonicalFare) -> SafeString:
        return _badge(obj.quality_grade, _GRADE_COLOURS.get(obj.quality_grade, "#6b7280"))

    @admin.display(description="Flags")
    def flag_badges(self, obj: CanonicalFare) -> SafeString:
        if not obj.flags:
            return mark_safe("<span style='color:#9ca3af'>-</span>")
        palette = {
            FareFlag.PARTIAL_BREAKDOWN: "#0369a1",
            FareFlag.SOLD_OUT: "#6b7280",
            FareFlag.OUTLIER_HIGH: "#b45309",
            FareFlag.OUTLIER_LOW: "#b45309",
            FareFlag.TOTAL_MISMATCH: "#b91c1c",
            FareFlag.DUPLICATE_SUPPRESSED: "#6b7280",
            FareFlag.CROSS_SOURCE_CONFLICT: "#7c3aed",
        }
        return format_html_join(
            " ",
            '<span style="background:{};color:#fff;padding:1px 6px;border-radius:8px;font-size:10px">{}</span>',
            ((palette.get(flag, "#334155"), flag.replace("_", " ").lower()) for flag in obj.flags),
        )

    @admin.display(description="Raw payload")
    def raw_observation_link(self, obj: CanonicalFare) -> SafeString:
        url = reverse("admin:apix_rawobservation_change", args=[obj.raw_observation_id])
        return format_html('<a href="{}">{}</a>', url, obj.raw_observation.ingress_hash[:24])

    @admin.display(description="Cleaning lineage")
    def provenance_preview(self, obj: CanonicalFare) -> SafeString:
        return _pretty_json(obj.provenance)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
@admin.register(IndexValue)
class IndexValueAdmin(admin.ModelAdmin):
    list_display = ("index_date", "scope_display", "window_display", "index_value",
                    "pop_display", "coverage_display", "published_badge", "hash_display")
    list_filter = ("methodology_code", "is_published", "lead_window_days", "route__code")
    search_fields = ("provenance_hash", "previous_hash", "route__code")
    date_hierarchy = "index_date"
    ordering = ("-index_date",)
    list_select_related = ("route",)
    actions = ("action_verify_chain", "action_suppress", "action_publish")
    readonly_fields = (
        "provenance_hash", "previous_hash", "inputs_root", "raw_observation_count",
        "mean_log_relative", "period_on_period_pct", "year_on_year_pct",
        "computed_at", "published_at", "metadata_preview",
    )
    fieldsets = (
        ("Series point", {"fields": ("methodology_code", "index_date", "lead_window_days", "route")}),
        ("Level", {"fields": ("index_value", "base_period", "base_index_value",
                              "mean_log_relative", "period_on_period_pct", "year_on_year_pct")}),
        ("Coverage", {"fields": ("n_observations", "n_matched_pairs", "n_routes",
                                 "n_outliers_excluded", "weight_coverage")}),
        ("Provenance", {"fields": ("provenance_hash", "previous_hash", "inputs_root",
                                   "raw_observation_count", "metadata_preview")}),
        ("Publication", {"fields": ("is_published", "suppression_reason", "computed_at", "published_at")}),
    )

    @admin.display(description="Scope", ordering="route__code")
    def scope_display(self, obj: IndexValue) -> SafeString:
        return _badge(obj.scope, "#1d4ed8" if obj.route_id is None else "#334155")

    @admin.display(description="Window", ordering="lead_window_days")
    def window_display(self, obj: IndexValue) -> str:
        return f"T+{obj.lead_window_days}" if obj.lead_window_days else "ALL"

    @admin.display(description="DoD %", ordering="period_on_period_pct")
    def pop_display(self, obj: IndexValue) -> SafeString:
        if obj.period_on_period_pct is None:
            return mark_safe("-")
        colour = "#b91c1c" if obj.period_on_period_pct > 0 else "#15803d"
        return format_html('<span style="color:{}">{:+.2f}%</span>', colour, obj.period_on_period_pct)

    @admin.display(description="Weight coverage", ordering="weight_coverage")
    def coverage_display(self, obj: IndexValue) -> SafeString:
        if obj.weight_coverage is None:
            return mark_safe("-")
        colour = "#15803d" if obj.weight_coverage >= 0.6 else "#b45309"
        return format_html('<span style="color:{}">{:.1%}</span>', colour, obj.weight_coverage)

    @admin.display(description="Published", boolean=True, ordering="is_published")
    def published_badge(self, obj: IndexValue) -> bool:
        return obj.is_published

    @admin.display(description="Provenance")
    def hash_display(self, obj: IndexValue) -> str:
        return obj.provenance_hash[:16]

    @admin.display(description="Computation metadata")
    def metadata_preview(self, obj: IndexValue) -> SafeString:
        return _pretty_json(obj.computation_metadata)

    @admin.action(description="Verify the SHA-256 provenance chain for these series")
    def action_verify_chain(self, request: HttpRequest, queryset: QuerySet[IndexValue]) -> None:
        scopes = {(row.route_id, row.lead_window_days) for row in queryset}
        for route_id, window in scopes:
            series = list(
                IndexValue.objects.filter(
                    route_id=route_id, lead_window_days=window, is_published=True
                ).order_by("index_date")
            )
            verification = verify_series(series)
            label = f"{series[0].scope if series else '?'} T+{window}"
            if verification.intact:
                self.message_user(
                    request, f"{label}: chain intact across {verification.verified} values.",
                    messages.SUCCESS,
                )
            else:
                self.message_user(
                    request,
                    f"{label}: CHAIN BROKEN at {verification.broken_at} - {verification.reason}",
                    messages.ERROR,
                )

    @admin.action(description="Suppress selected values (withdraw from publication)")
    def action_suppress(self, request: HttpRequest, queryset: QuerySet[IndexValue]) -> None:
        for value in queryset:
            value.is_published = False
            value.suppression_reason = f"manually suppressed by {request.user.get_username()}"
            value.save(update_fields=["is_published", "suppression_reason"])
            AuditLog.record(
                AuditAction.INDEX_SUPPRESSED, summary=f"{value} suppressed",
                entity=value, actor=request.user,
            )
        self.message_user(request, f"Suppressed {queryset.count()} value(s).", messages.WARNING)

    @admin.action(description="Publish selected values")
    def action_publish(self, request: HttpRequest, queryset: QuerySet[IndexValue]) -> None:
        for value in queryset:
            value.is_published = True
            value.suppression_reason = ""
            value.save(update_fields=["is_published", "suppression_reason"])
            AuditLog.record(
                AuditAction.INDEX_PUBLISHED, summary=f"{value} published",
                entity=value, actor=request.user,
            )
        self.message_user(request, f"Published {queryset.count()} value(s).", messages.SUCCESS)


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
@admin.register(CollectionRun)
class CollectionRunAdmin(admin.ModelAdmin):
    list_display = ("short_id", "status_badge", "trigger_reason", "triggered_by",
                    "raw_count", "canonical_count", "duplicate_count", "outlier_count",
                    "duration_display", "created_at")
    list_filter = ("status", "trigger_reason")
    search_fields = ("id", "celery_task_id", "correlation_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    actions = ("action_rerun",)
    readonly_fields = ("id", "celery_task_id", "correlation_id", "started_at", "finished_at",
                       "raw_count", "canonical_count", "duplicate_count", "outlier_count",
                       "rejected_count", "stats_preview", "error", "created_at")
    filter_horizontal = ("routes",)
    exclude = ("stats",)

    @admin.display(description="Run", ordering="id")
    def short_id(self, obj: CollectionRun) -> str:
        return str(obj.id)[:8]

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj: CollectionRun) -> SafeString:
        colours = {
            CollectionStatus.SUCCEEDED: "#15803d",
            CollectionStatus.PARTIAL: "#b45309",
            CollectionStatus.RUNNING: "#0369a1",
            CollectionStatus.PENDING: "#6b7280",
            CollectionStatus.FAILED: "#b91c1c",
            CollectionStatus.BLOCKED: "#b91c1c",
            CollectionStatus.CANCELLED: "#6b7280",
        }
        return _badge(obj.status, colours.get(obj.status, "#6b7280"))

    @admin.display(description="Duration")
    def duration_display(self, obj: CollectionRun) -> str:
        seconds = obj.duration_seconds
        return f"{seconds:.1f}s" if seconds is not None else "-"

    @admin.display(description="Per-stage statistics")
    def stats_preview(self, obj: CollectionRun) -> SafeString:
        return _pretty_json(obj.stats)

    @admin.action(description="Re-run with the same parameters")
    def action_rerun(self, request: HttpRequest, queryset: QuerySet[CollectionRun]) -> None:
        for run in queryset:
            parameters = run.parameters or {}
            dispatch_daily_basket(
                route_codes=parameters.get("route_codes"),
                lead_windows=parameters.get("lead_windows"),
                source_codes=parameters.get("source_codes"),
                triggered_by_id=request.user.pk,
                reason=f"rerun:{str(run.id)[:8]}",
            )
        self.message_user(request, f"Queued {queryset.count()} re-run(s).", messages.SUCCESS)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_label", "entity_type", "entity_id", "summary")
    list_filter = ("action", "entity_type")
    search_fields = ("summary", "entity_id", "correlation_id", "actor_label")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("action", "actor", "actor_label", "entity_type", "entity_id",
                       "summary", "before_preview", "after_preview", "context_preview",
                       "correlation_id", "created_at")
    exclude = ("before", "after", "context")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: AuditLog | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: AuditLog | None = None) -> bool:
        return False

    @admin.display(description="Before")
    def before_preview(self, obj: AuditLog) -> SafeString:
        return _pretty_json(obj.before)

    @admin.display(description="After")
    def after_preview(self, obj: AuditLog) -> SafeString:
        return _pretty_json(obj.after)

    @admin.display(description="Context")
    def context_preview(self, obj: AuditLog) -> SafeString:
        return _pretty_json(obj.context)
