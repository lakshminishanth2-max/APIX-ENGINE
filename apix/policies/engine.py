"""Compliance policy engine.

Three guards, composed, consulted before every outbound request:

1. **robots.txt** - fetched over HTTP, parsed with :mod:`urllib.robotparser`,
   cached per origin in Redis.  Evaluated per RFC 9309: a ``4xx`` means the file
   is *unavailable* and access is permitted; a ``5xx`` or a transport failure
   means it is *unreachable*, which we treat as a full disallow whenever
   ``ROBOTS_FAIL_CLOSED`` is set.  We never assume consent we could not verify.
   Fractional ``Crawl-delay`` directives are parsed by hand because the stdlib
   parser silently discards them (it calls ``int()``).

2. **Distributed token bucket** - :mod:`apix.policies.bucket`, so the agreed
   request rate holds across the whole worker fleet rather than per process.

3. **Circuit breaker** - persisted on :class:`~apix.models.SourcePolicy` so a
   trip survives worker restarts and is visible in the admin.  Ordinary
   transport failures follow the conventional CLOSED -> OPEN -> HALF_OPEN cycle.
   A **bot challenge is different**: it latches ``TRIPPED_PERMANENT`` and no
   timer will ever clear it.  Only :meth:`PolicyEngine.resume` - an audited
   operator action - reopens the source.
"""

from __future__ import annotations

import logging
import re
import urllib.robotparser
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final
from re import Pattern
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apix.enums import AuditAction, CircuitState, ComplianceStatus
from apix.models import AuditLog, Source, SourcePolicy
from apix.policies.bucket import BucketGrant, RateLimitTimeout, get_token_bucket
from apix.policies.challenge import BotChallengeDetected, BotChallengeDetector, BotChallengeSignal

__all__ = [
    "CircuitOpen",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyGrant",
    "PolicyViolation",
    "RobotsDisallowed",
    "SourceHalted",
    "get_policy_engine",
]

logger = logging.getLogger(__name__)

_ROBOTS_CACHE_PREFIX: Final[str] = "apix:robots:"
_ROBOTS_MAX_BYTES: Final[int] = 512 * 1024
_CRAWL_DELAY_RE: Final[Pattern[str]] = re.compile(r"^\s*crawl-delay\s*:\s*([0-9]*\.?[0-9]+)", re.I)
_USER_AGENT_RE: Final[Pattern[str]] = re.compile(r"^\s*user-agent\s*:\s*(.+?)\s*$", re.I)


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
class PolicyDecision:
    ALLOW = "ALLOW"
    DENY_ROBOTS = "DENY_ROBOTS"
    DENY_ROBOTS_UNREACHABLE = "DENY_ROBOTS_UNREACHABLE"
    DENY_RATE_LIMIT = "DENY_RATE_LIMIT"
    DENY_CIRCUIT_OPEN = "DENY_CIRCUIT_OPEN"
    DENY_HALTED = "DENY_HALTED"
    DENY_DISABLED = "DENY_DISABLED"


class PolicyViolation(RuntimeError):
    """Refusal.  ``policy_blocked`` is the marker the collector base reads."""

    policy_blocked: bool = True
    halted: bool = False
    decision: str = PolicyDecision.DENY_DISABLED

    def __init__(self, message: str, *, source_code: str = "", url: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.source_code = source_code
        self.url = url
        self.retry_after = retry_after


class RobotsDisallowed(PolicyViolation):
    decision = PolicyDecision.DENY_ROBOTS


class RobotsUnreachable(PolicyViolation):
    decision = PolicyDecision.DENY_ROBOTS_UNREACHABLE


class CircuitOpen(PolicyViolation):
    decision = PolicyDecision.DENY_CIRCUIT_OPEN


class SourceHalted(CircuitOpen):
    """Terminal state; requires an operator to clear."""

    halted = True
    decision = PolicyDecision.DENY_HALTED


@dataclass(frozen=True, slots=True)
class RobotsVerdict:
    allowed: bool
    reason: str
    crawl_delay: float | None = None
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class PolicyGrant:
    """Positive authorisation receipt; attached to the run's statistics."""

    source_code: str
    url: str
    decision: str = PolicyDecision.ALLOW
    waited_ms: int = 0
    tokens_remaining: float = 0.0
    crawl_delay: float | None = None
    robots_reason: str = "not_checked"


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #
def parse_crawl_delays(body: str) -> dict[str, float]:
    """Extract ``Crawl-delay`` per user-agent group, fractions included."""
    delays: dict[str, float] = {}
    current: list[str] = []
    previous_was_agent = False
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0]
        agent_match = _USER_AGENT_RE.match(line)
        if agent_match:
            if not previous_was_agent:
                current = []
            current.append(agent_match.group(1).strip().lower())
            previous_was_agent = True
            continue
        previous_was_agent = False
        delay_match = _CRAWL_DELAY_RE.match(line)
        if delay_match and current:
            value = float(delay_match.group(1))
            for agent in current:
                delays[agent] = max(delays.get(agent, 0.0), value)
    return delays


def delay_for_agent(delays: Mapping[str, float], user_agent: str) -> float | None:
    """Most specific matching group wins; fall back to the ``*`` group."""
    agent = user_agent.lower()
    best: tuple[int, float] | None = None
    for token, delay in delays.items():
        if token == "*":
            continue
        if token and token in agent and (best is None or len(token) > best[0]):
            best = (len(token), delay)
    return best[1] if best is not None else delays.get("*")


class RobotsCache:
    """Origin-scoped robots.txt evaluation, cached in Redis."""

    def __init__(self, *, user_agent: str, ttl: int, timeout: int, fail_closed: bool) -> None:
        self._user_agent = user_agent
        self._ttl = ttl
        self._timeout = timeout
        self._fail_closed = fail_closed

    @staticmethod
    def origin_of(url: str) -> str:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"policy URL must be absolute: {url!r}")
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def evaluate(self, url: str, *, robots_url: str = "") -> RobotsVerdict:
        origin = self.origin_of(url)
        cache_key = f"{_ROBOTS_CACHE_PREFIX}{origin}"
        record: dict[str, Any] | None = cache.get(cache_key)
        if record is None:
            record = self._fetch(robots_url or f"{origin}/robots.txt")
            cache.set(cache_key, record, self._ttl if record["ok"] else max(60, self._ttl // 12))

        if record["mode"] == "disallow_all":
            return RobotsVerdict(False, record["reason"], None, record.get("status"))
        if record["mode"] == "allow_all":
            return RobotsVerdict(True, record["reason"], None, record.get("status"))

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(record["body"].splitlines())
        allowed = parser.can_fetch(self._user_agent, url)
        stdlib_delay = parser.crawl_delay(self._user_agent)
        delay = float(stdlib_delay) if stdlib_delay is not None else delay_for_agent(
            record.get("delays", {}), self._user_agent
        )
        return RobotsVerdict(
            allowed=allowed,
            reason="robots_allow" if allowed else "robots_disallow_rule",
            crawl_delay=delay,
            status_code=record.get("status"),
        )

    def _fetch(self, robots_url: str) -> dict[str, Any]:
        try:
            response = requests.get(
                robots_url,
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent, "Accept": "text/plain"},
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            logger.warning("robots fetch failed", extra={"url": robots_url, "error": str(exc)})
            return {
                "ok": False,
                "mode": "disallow_all" if self._fail_closed else "allow_all",
                "reason": f"robots_unreachable:{type(exc).__name__}",
                "status": None,
            }

        status = response.status_code
        if status == 200:
            body = response.text[:_ROBOTS_MAX_BYTES]
            return {
                "ok": True,
                "mode": "parse",
                "reason": "robots_parsed",
                "status": status,
                "body": body,
                "delays": parse_crawl_delays(body),
            }
        if status == 429 or status >= 500:
            # RFC 9309 "unreachable" - assume a complete disallow.
            return {
                "ok": False,
                "mode": "disallow_all" if self._fail_closed else "allow_all",
                "reason": f"robots_unreachable:http_{status}",
                "status": status,
            }
        # Any other 4xx: "unavailable" - access is permitted.
        return {"ok": True, "mode": "allow_all", "reason": f"robots_unavailable:http_{status}", "status": status}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class PolicyEngine:
    """Single gate every collector must pass through."""

    def __init__(self, *, user_agent: str | None = None) -> None:
        cfg = settings.APIX
        self.user_agent: str = user_agent or cfg["USER_AGENT"]
        self._respect_robots: bool = bool(cfg["RESPECT_ROBOTS"])
        self._bucket = get_token_bucket()
        self._robots = RobotsCache(
            user_agent=self.user_agent,
            ttl=int(cfg["ROBOTS_CACHE_TTL"]),
            timeout=int(cfg["ROBOTS_TIMEOUT"]),
            fail_closed=bool(cfg["ROBOTS_FAIL_CLOSED"]),
        )

    # -- gate --------------------------------------------------------------- #
    def authorize(self, *, source_code: str, url: str, tokens: int = 1) -> PolicyGrant:
        """Permit or refuse one outbound request."""
        policy = self._policy_for(source_code)

        # 1. Compliance / breaker state.
        self._assert_permitted(policy, source_code=source_code, url=url)

        # 2. robots.txt.
        verdict = RobotsVerdict(True, "robots_check_disabled")
        if self._respect_robots and policy.respect_robots:
            verdict = self._robots.evaluate(url, robots_url=policy.robots_url)
            self._remember_robots(policy, verdict)
            if not verdict.allowed:
                error_cls = (
                    RobotsUnreachable if verdict.reason.startswith("robots_unreachable") else RobotsDisallowed
                )
                AuditLog.record(
                    AuditAction.ROBOTS_DISALLOWED,
                    summary=f"{source_code}: {verdict.reason}",
                    entity=policy.source,
                    context={"url": url, "reason": verdict.reason, "status": verdict.status_code},
                )
                raise error_cls(
                    f"robots.txt forbids {url!r} for {self.user_agent!r} ({verdict.reason})",
                    source_code=source_code,
                    url=url,
                )

        # 3. Rate limit, tightened by any Crawl-delay the host published.
        min_interval = float(policy.crawl_delay_seconds or 0.0)
        if verdict.crawl_delay:
            min_interval = max(min_interval, float(verdict.crawl_delay))

        try:
            grant: BucketGrant = self._bucket.acquire(
                source_code,
                rate_per_second=policy.refill_rate_per_second,
                capacity=float(policy.burst),
                tokens=float(tokens),
                min_interval=min_interval,
            )
        except RateLimitTimeout as exc:
            AuditLog.record(
                AuditAction.RATE_LIMIT_EXCEEDED,
                summary=f"{source_code}: rate budget exhausted",
                entity=policy.source,
                context={"url": url, "waited": exc.waited, "budget": exc.budget},
            )
            error = PolicyViolation(str(exc), source_code=source_code, url=url, retry_after=exc.budget)
            error.decision = PolicyDecision.DENY_RATE_LIMIT
            raise error from exc

        return PolicyGrant(
            source_code=source_code,
            url=url,
            waited_ms=grant.waited_ms,
            tokens_remaining=grant.tokens_remaining,
            crawl_delay=min_interval or None,
            robots_reason=verdict.reason,
        )

    # -- feedback ----------------------------------------------------------- #
    def observe_response(
        self,
        *,
        source_code: str,
        url: str,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        body_snippet: str | None = None,
    ) -> None:
        """Feed a response back in.  A challenge halts the source immediately."""
        signal = BotChallengeDetector.detect(
            status_code=status_code, headers=headers, body_snippet=body_snippet
        )
        if signal is not None:
            self.halt(source_code, signal, url=url)
            raise BotChallengeDetected(signal, source_code=source_code)

        if status_code >= 500 or status_code == 429:
            self._record_failure(source_code, f"http_{status_code}")
        else:
            self._record_success(source_code)

    def observe_exception(self, *, source_code: str, url: str, error: BaseException) -> None:
        if isinstance(error, BotChallengeDetected):
            self.halt(source_code, error.signal, url=url)
            return
        self._record_failure(source_code, f"{type(error).__name__}: {error}"[:500])

    # -- breaker ------------------------------------------------------------ #
    def halt(self, source_code: str, signal: BotChallengeSignal, *, url: str = "") -> None:
        """Latch a source off permanently after a confirmed challenge."""
        with transaction.atomic():
            policy = SourcePolicy.objects.select_for_update().select_related("source").get(
                source__code=source_code
            )
            if policy.circuit_state == CircuitState.TRIPPED_PERMANENT:
                return
            policy.halt(signal.describe())
            AuditLog.record(
                AuditAction.BOT_CHALLENGE_DETECTED,
                summary=f"{source_code} halted: {signal.vendor}",
                entity=policy.source,
                after={"compliance_status": policy.compliance_status, "circuit_state": policy.circuit_state},
                context={
                    "url": url,
                    "vendor": signal.vendor,
                    "evidence": signal.evidence,
                    "confidence": signal.confidence,
                    "status_code": signal.status_code,
                },
            )
        logger.error(
            "SOURCE HALTED - bot challenge detected; no evasion will be attempted",
            extra={"source": source_code, "vendor": signal.vendor, "evidence": signal.evidence},
        )

    def resume(self, source_code: str, *, actor: Any = None, force: bool = False, note: str = "") -> bool:
        """Operator action.  ``force`` is required to clear a permanent trip."""
        with transaction.atomic():
            policy = SourcePolicy.objects.select_for_update().select_related("source").get(
                source__code=source_code
            )
            if policy.circuit_state == CircuitState.TRIPPED_PERMANENT and not force:
                return False
            before = {"compliance_status": policy.compliance_status, "circuit_state": policy.circuit_state}
            policy.compliance_status = ComplianceStatus.ACTIVE
            policy.circuit_state = CircuitState.CLOSED
            policy.consecutive_failures = 0
            policy.opened_at = None
            policy.halted_at = None
            policy.halt_reason = ""
            policy.save(update_fields=[
                "compliance_status", "circuit_state", "consecutive_failures",
                "opened_at", "halted_at", "halt_reason", "updated_at",
            ])
            AuditLog.record(
                AuditAction.SOURCE_RESUMED,
                summary=f"{source_code} resumed by operator",
                entity=policy.source,
                actor=actor,
                before=before,
                after={"compliance_status": policy.compliance_status, "circuit_state": policy.circuit_state},
                context={"force": force, "note": note},
            )
        self._bucket.reset(source_code)
        return True

    # -- introspection ------------------------------------------------------ #
    def snapshot(self, source_code: str) -> dict[str, Any]:
        policy = self._policy_for(source_code)
        return {
            "source_code": source_code,
            "compliance_status": policy.compliance_status,
            "circuit_state": policy.circuit_state,
            "consecutive_failures": policy.consecutive_failures,
            "halted_at": policy.halted_at.isoformat() if policy.halted_at else None,
            "halt_reason": policy.halt_reason,
            "requests_per_minute": policy.requests_per_minute,
            "bucket": self._bucket.inspect(
                source_code,
                rate_per_second=policy.refill_rate_per_second,
                capacity=float(policy.burst),
            ),
            "robots": {
                "respected": policy.respect_robots,
                "last_verdict": policy.robots_last_verdict,
                "last_checked": policy.robots_last_checked.isoformat() if policy.robots_last_checked else None,
                "crawl_delay": str(policy.crawl_delay_seconds) if policy.crawl_delay_seconds else None,
            },
        }

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _policy_for(source_code: str) -> SourcePolicy:
        try:
            return SourcePolicy.objects.select_related("source").get(source__code=source_code)
        except SourcePolicy.DoesNotExist:
            source = Source.objects.filter(code=source_code).first()
            if source is None:
                raise PolicyViolation(
                    f"source {source_code!r} is not registered in the database", source_code=source_code
                ) from None
            cfg = settings.APIX
            return SourcePolicy.objects.create(
                source=source,
                requests_per_minute=cfg["DEFAULT_REQUESTS_PER_MINUTE"],
                burst=cfg["DEFAULT_BURST"],
                user_agent=cfg["USER_AGENT"],
                failure_threshold=cfg["BREAKER_FAILURE_THRESHOLD"],
                cooldown_seconds=cfg["BREAKER_COOLDOWN_SECONDS"],
            )

    @staticmethod
    def _assert_permitted(policy: SourcePolicy, *, source_code: str, url: str) -> None:
        if policy.circuit_state == CircuitState.TRIPPED_PERMANENT:
            raise SourceHalted(
                f"source {source_code!r} is halted ({policy.halt_reason or 'operator action'}); "
                "an operator must resume it",
                source_code=source_code,
                url=url,
            )
        if policy.compliance_status in ComplianceStatus.blocking():
            error = PolicyViolation(
                f"source {source_code!r} is {policy.compliance_status}",
                source_code=source_code,
                url=url,
            )
            error.decision = PolicyDecision.DENY_DISABLED
            raise error
        if not policy.source.is_active:
            error = PolicyViolation(f"source {source_code!r} is inactive", source_code=source_code, url=url)
            error.decision = PolicyDecision.DENY_DISABLED
            raise error

        if policy.circuit_state == CircuitState.OPEN:
            opened = policy.opened_at or timezone.now()
            reopen_at = opened + timedelta(seconds=policy.cooldown_seconds)
            if timezone.now() < reopen_at:
                remaining = (reopen_at - timezone.now()).total_seconds()
                raise CircuitOpen(
                    f"circuit open for {source_code!r}; retry in {remaining:.0f}s",
                    source_code=source_code,
                    url=url,
                    retry_after=remaining,
                )
            SourcePolicy.objects.filter(pk=policy.pk).update(circuit_state=CircuitState.HALF_OPEN)
            policy.circuit_state = CircuitState.HALF_OPEN

    @staticmethod
    def _remember_robots(policy: SourcePolicy, verdict: RobotsVerdict) -> None:
        updates: dict[str, Any] = {
            "robots_last_checked": timezone.now(),
            "robots_last_verdict": verdict.reason[:64],
        }
        if verdict.crawl_delay is not None:
            updates["crawl_delay_seconds"] = Decimal(str(verdict.crawl_delay))
        SourcePolicy.objects.filter(pk=policy.pk).update(**updates)

    def _record_success(self, source_code: str) -> None:
        with transaction.atomic():
            policy = SourcePolicy.objects.select_for_update().get(source__code=source_code)
            if policy.circuit_state == CircuitState.TRIPPED_PERMANENT:
                return
            policy.consecutive_failures = 0
            policy.circuit_state = CircuitState.CLOSED
            policy.opened_at = None
            policy.last_success_at = timezone.now()
            policy.total_requests += 1
            policy.save(update_fields=[
                "consecutive_failures", "circuit_state", "opened_at",
                "last_success_at", "total_requests", "updated_at",
            ])

    def _record_failure(self, source_code: str, reason: str) -> None:
        with transaction.atomic():
            policy = SourcePolicy.objects.select_for_update().select_related("source").get(
                source__code=source_code
            )
            if policy.circuit_state == CircuitState.TRIPPED_PERMANENT:
                return
            policy.consecutive_failures += 1
            policy.total_failures += 1
            policy.total_requests += 1
            tripped = (
                policy.circuit_state == CircuitState.HALF_OPEN
                or policy.consecutive_failures >= policy.failure_threshold
            )
            if tripped:
                policy.circuit_state = CircuitState.OPEN
                policy.opened_at = timezone.now()
            policy.save(update_fields=[
                "consecutive_failures", "total_failures", "total_requests",
                "circuit_state", "opened_at", "updated_at",
            ])
            if tripped:
                AuditLog.record(
                    AuditAction.SOURCE_HALTED,
                    summary=f"{source_code}: circuit opened after {policy.consecutive_failures} failures",
                    entity=policy.source,
                    context={"reason": reason, "cooldown_seconds": policy.cooldown_seconds},
                )
                logger.warning(
                    "circuit opened",
                    extra={"source": source_code, "failures": policy.consecutive_failures, "reason": reason},
                )


_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    """Process-wide engine.  Cheap to construct; state lives in Redis and PG."""
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine
