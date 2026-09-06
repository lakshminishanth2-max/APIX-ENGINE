"""Signal receivers.

Kept deliberately thin: signals are used only for cross-cutting bookkeeping that
must happen no matter which code path made the change (admin, API, task or
shell).  Domain logic lives in the services, never here.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apix.enums import AuditAction, ComplianceStatus
from apix.models import AuditLog, IndexValue, Source, SourcePolicy

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Source, dispatch_uid="apix.source.ensure_policy")
def ensure_source_policy(sender: type[Source], instance: Source, created: bool, **_: object) -> None:
    """Every source must have a compliance envelope from the moment it exists.

    A source without a policy row would fall through the rate limiter, so the
    default is created here rather than being left to whoever adds the row.
    """
    if not created:
        return
    cfg = settings.APIX
    SourcePolicy.objects.get_or_create(
        source=instance,
        defaults={
            "requests_per_minute": cfg["DEFAULT_REQUESTS_PER_MINUTE"],
            "burst": cfg["DEFAULT_BURST"],
            "user_agent": cfg["USER_AGENT"],
            "failure_threshold": cfg["BREAKER_FAILURE_THRESHOLD"],
            "cooldown_seconds": cfg["BREAKER_COOLDOWN_SECONDS"],
            "robots_url": f"{instance.base_url.rstrip('/')}/robots.txt" if instance.base_url else "",
        },
    )


@receiver(pre_save, sender=SourcePolicy, dispatch_uid="apix.policy.audit_status")
def audit_compliance_change(sender: type[SourcePolicy], instance: SourcePolicy, **_: object) -> None:
    """Record every compliance-status transition, whoever made it."""
    if instance.pk is None:
        return
    previous = SourcePolicy.objects.filter(pk=instance.pk).values(
        "compliance_status", "circuit_state"
    ).first()
    if not previous:
        return
    if previous["compliance_status"] == instance.compliance_status:
        return

    action = (
        AuditAction.SOURCE_RESUMED
        if instance.compliance_status == ComplianceStatus.ACTIVE
        else AuditAction.SOURCE_HALTED
    )
    AuditLog.record(
        action,
        summary=f"{instance.source_id}: {previous['compliance_status']} -> {instance.compliance_status}",
        entity=instance.source if instance.source_id else None,
        before=previous,
        after={"compliance_status": instance.compliance_status, "circuit_state": instance.circuit_state},
    )


@receiver(post_save, sender=IndexValue, dispatch_uid="apix.index.log_publication")
def log_index_publication(sender: type[IndexValue], instance: IndexValue, created: bool, **_: object) -> None:
    """Publication is a notable event even when the value is unchanged."""
    if not instance.is_published:
        return
    logger.info(
        "index value published",
        extra={
            "scope": instance.scope,
            "lead_window": instance.lead_window_days,
            "index_date": instance.index_date.isoformat(),
            "index_value": str(instance.index_value),
            "provenance_hash": instance.provenance_hash[:16],
        },
    )
