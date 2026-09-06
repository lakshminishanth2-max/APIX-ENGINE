"""Compliance and safety policy engine for the APIx collection tier.

Three independent guards are composed into a single :class:`PolicyEngine`,
which every collector must consult before touching a remote host:

1. **robots.txt** - fetched asynchronously, parsed with the stdlib
   :mod:`urllib.robotparser`, cached per host with a TTL, negative-cached, and
   evaluated per RFC 9309 (``5xx`` / transport failure means *unreachable* and
   is therefore treated as a full disallow when ``fail_closed`` is set).
2. **Token-bucket rate limiting** - per host, monotonic-clock based, FIFO fair
   (the bucket lock is held across the wait so callers are served in arrival
   order), and automatically tightened to honour a robots ``Crawl-delay``.
3. **Fail-safe circuit breaker** - ordinary transport failures trip a
   conventional CLOSED -> OPEN -> HALF_OPEN cycle, but any *bot challenge*
   (CAPTCHA, Cloudflare/DataDome/Akamai interstitial, WAF block) latches the
   breaker into a terminal ``HALTED`` state.  A halted source never resumes on
   a timer: it requires an explicit operator :meth:`PolicyEngine.resume` call.
   Detecting that a site is actively challenging us is treated as a hard stop,
   never as something to retry through.

The engine structurally satisfies ``app.collectors.base.PolicyGuard``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.robotparser
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BotChallengeSignal",
    "BreakerConfig",
    "BreakerSnapshot",
    "BreakerState",
    "BotChallengeDetector",
    "CircuitBreaker",
    "CircuitOpenError",
    "HostRatePolicy",
    "PolicyConfig",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyGrant",
    "PolicyViolation",
    "RateLimitTimeout",
    "RobotsCache",
    "RobotsDisallowed",
    "RobotsVerdict",
    "SourceHalted",
    "TokenBucket",
]

logger = logging.getLogger(__name__)

_UTC: Final = timezone.utc
_ROBOTS_MAX_BYTES: Final[int] = 512 * 1024
_BODY_SNIPPET_LIMIT: Final[int] = 8192


# --------------------------------------------------------------------------- #
# Decisions and errors
# --------------------------------------------------------------------------- #
class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY_KILL_SWITCH = "deny_kill_switch"
    DENY_SOURCE_DISABLED = "deny_source_disabled"
    DENY_ROBOTS = "deny_robots"
    DENY_ROBOTS_UNREACHABLE = "deny_robots_unreachable"
    DENY_RATE_LIMIT = "deny_rate_limit"
    DENY_CIRCUIT_OPEN = "deny_circuit_open"
    DENY_HALTED = "deny_halted"


class PolicyViolation(RuntimeError):
    """Raised whenever the engine refuses a request.

    ``policy_blocked`` is the duck-typed marker read by
    ``BaseCollector._classify`` so the ingestion tier never has to import this
    module.
    """

    policy_blocked: bool = True
    circuit_open: bool = False
    decision: PolicyDecision = PolicyDecision.DENY_KILL_SWITCH

    def __init__(
        self,
        message: str,
        *,
        source_id: str | None = None,
        url: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.source_id = source_id
        self.url = url
        self.retry_after_s = retry_after_s


class RobotsDisallowed(PolicyViolation):
    decision = PolicyDecision.DENY_ROBOTS


class RobotsUnreachable(PolicyViolation):
    decision = PolicyDecision.DENY_ROBOTS_UNREACHABLE


class RateLimitTimeout(PolicyViolation):
    decision = PolicyDecision.DENY_RATE_LIMIT


class CircuitOpenError(PolicyViolation):
    circuit_open = True
    decision = PolicyDecision.DENY_CIRCUIT_OPEN


class SourceHalted(CircuitOpenError):
    """Terminal state: a bot challenge was observed and never auto-recovers."""

    decision = PolicyDecision.DENY_HALTED


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class HostRatePolicy(BaseModel):
    """Token-bucket parameters for one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests_per_second: float = Field(default=0.5, gt=0.0, le=100.0)
    burst: float = Field(default=2.0, ge=1.0, le=500.0)
    #: Floor applied to any robots ``Crawl-delay`` we honour.
    min_crawl_delay_s: float = Field(default=0.0, ge=0.0, le=600.0)
    #: Ceiling: a hostile ``Crawl-delay: 86400`` must not wedge the scheduler.
    max_crawl_delay_s: float = Field(default=60.0, ge=0.0, le=3_600.0)


class BreakerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_threshold: int = Field(default=5, ge=1, le=100)
    success_threshold: int = Field(default=2, ge=1, le=50)
    recovery_timeout_s: float = Field(default=120.0, gt=0.0, le=86_400.0)
    half_open_max_calls: int = Field(default=1, ge=1, le=10)
    #: HTTP 429 alone is a rate signal, not proof of a challenge.
    halt_on_http_429: bool = False


class PolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_agent: str = "APIxBot/1.0 (+https://apix.example/bot)"
    #: Global kill switch - flip to ``False`` to stop all outbound collection.
    enabled: bool = True
    respect_robots: bool = True
    #: When robots.txt cannot be retrieved, refuse rather than assume consent.
    fail_closed: bool = True
    robots_ttl_s: float = Field(default=3_600.0, gt=0.0)
    robots_error_ttl_s: float = Field(default=300.0, gt=0.0)
    robots_timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)
    acquire_timeout_s: float = Field(default=30.0, gt=0.0, le=600.0)
    default_rate: HostRatePolicy = HostRatePolicy()
    host_overrides: Mapping[str, HostRatePolicy] = Field(default_factory=dict)
    breaker: BreakerConfig = BreakerConfig()
    disabled_sources: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RobotsVerdict:
    """Outcome of a robots.txt evaluation for one URL."""

    allowed: bool
    reason: str
    crawl_delay_s: float | None = None
    status_code: int | None = None
    stale: bool = False


@dataclass(slots=True)
class _RobotsEntry:
    parser: urllib.robotparser.RobotFileParser | None
    allow_all: bool
    disallow_all: bool
    status_code: int | None
    expires_at: float
    reason: str
    #: user-agent token (lower-case, "*" for the default group) -> delay seconds
    crawl_delays: Mapping[str, float] = field(default_factory=dict)


_CRAWL_DELAY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*crawl-delay\s*:\s*([0-9]*\.?[0-9]+)", re.I)
_USER_AGENT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*user-agent\s*:\s*(.+?)\s*$", re.I)


def _parse_crawl_delays(body: str) -> dict[str, float]:
    """Extract ``Crawl-delay`` per user-agent group.

    ``urllib.robotparser`` silently drops fractional delays (it calls ``int``),
    yet ``Crawl-delay: 0.5`` is common in the wild.  Parsing the directive
    ourselves means a host asking to be crawled slowly is always obeyed.
    """
    delays: dict[str, float] = {}
    current: list[str] = []
    previous_was_agent = False
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0]
        agent_match = _USER_AGENT_RE.match(line)
        if agent_match:
            if not previous_was_agent:
                current = []
            current.append(agent_match.group(1).lower())
            previous_was_agent = True
            continue
        previous_was_agent = False
        delay_match = _CRAWL_DELAY_RE.match(line)
        if delay_match and current:
            try:
                value = float(delay_match.group(1))
            except ValueError:  # pragma: no cover - regex guarantees a number
                continue
            for agent in current:
                delays[agent] = max(delays.get(agent, 0.0), value)
    return delays


def _delay_for_agent(delays: Mapping[str, float], user_agent: str) -> float | None:
    """Most specific matching group wins, falling back to the ``*`` group."""
    agent = user_agent.lower()
    best: tuple[int, float] | None = None
    for token, delay in delays.items():
        if token == "*":
            continue
        if token and token in agent and (best is None or len(token) > best[0]):
            best = (len(token), delay)
    if best is not None:
        return best[1]
    return delays.get("*")


class RobotsCache:
    """Async robots.txt fetcher with per-origin TTL cache and stampede control."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        user_agent: str,
        ttl_s: float = 3_600.0,
        error_ttl_s: float = 300.0,
        timeout_s: float = 10.0,
        fail_closed: bool = True,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._ttl_s = ttl_s
        self._error_ttl_s = error_ttl_s
        self._timeout_s = timeout_s
        self._fail_closed = fail_closed
        self._entries: dict[str, _RobotsEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def origin_of(url: str) -> str:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"policy URL must be absolute: {url!r}")
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    async def evaluate(self, url: str, *, user_agent: str | None = None) -> RobotsVerdict:
        """Return whether ``url`` may be fetched under the configured agent."""
        agent = user_agent or self._user_agent
        origin = self.origin_of(url)
        entry = await self._entry_for(origin)

        if entry.disallow_all:
            return RobotsVerdict(False, entry.reason, None, entry.status_code)
        if entry.allow_all or entry.parser is None:
            return RobotsVerdict(True, entry.reason, None, entry.status_code)

        allowed = entry.parser.can_fetch(agent, url)
        delay = entry.parser.crawl_delay(agent)
        crawl_delay = float(delay) if delay is not None else _delay_for_agent(entry.crawl_delays, agent)
        return RobotsVerdict(
            allowed=allowed,
            reason="robots_allow" if allowed else "robots_disallow_rule",
            crawl_delay_s=crawl_delay,
            status_code=entry.status_code,
        )

    def invalidate(self, url_or_origin: str) -> None:
        origin = self.origin_of(url_or_origin)
        self._entries.pop(origin, None)

    # -- internals ---------------------------------------------------------- #
    async def _entry_for(self, origin: str) -> _RobotsEntry:
        now = time.monotonic()
        cached = self._entries.get(origin)
        if cached is not None and cached.expires_at > now:
            return cached

        async with self._guard:
            lock = self._locks.setdefault(origin, asyncio.Lock())

        async with lock:
            # Another task may have refreshed while we waited for the lock.
            cached = self._entries.get(origin)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached
            entry = await self._fetch(origin)
            self._entries[origin] = entry
            return entry

    async def _fetch(self, origin: str) -> _RobotsEntry:
        robots_url = f"{origin}/robots.txt"
        now = time.monotonic()
        try:
            response = await self._client.get(
                robots_url,
                timeout=self._timeout_s,
                headers={"User-Agent": self._user_agent, "Accept": "text/plain"},
                follow_redirects=True,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            logger.warning("robots fetch failed origin=%s error=%s", origin, exc)
            return _RobotsEntry(
                parser=None,
                allow_all=not self._fail_closed,
                disallow_all=self._fail_closed,
                status_code=None,
                expires_at=now + self._error_ttl_s,
                reason=f"robots_unreachable:{type(exc).__name__}",
            )

        status = response.status_code
        if status == 200:
            body = response.text[:_ROBOTS_MAX_BYTES]
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(body.splitlines())
            return _RobotsEntry(
                parser=parser,
                allow_all=False,
                disallow_all=False,
                status_code=status,
                expires_at=now + self._ttl_s,
                reason="robots_parsed",
                crawl_delays=_parse_crawl_delays(body),
            )

        if status == 429 or status >= 500:
            # RFC 9309: "unreachable" - assume complete disallow.
            return _RobotsEntry(
                parser=None,
                allow_all=not self._fail_closed,
                disallow_all=self._fail_closed,
                status_code=status,
                expires_at=now + self._error_ttl_s,
                reason=f"robots_unreachable:http_{status}",
            )

        # RFC 9309: any other 4xx means "unavailable" -> access is permitted.
        return _RobotsEntry(
            parser=None,
            allow_all=True,
            disallow_all=False,
            status_code=status,
            expires_at=now + self._ttl_s,
            reason=f"robots_unavailable:http_{status}",
        )


# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #
class TokenBucket:
    """Monotonic-clock token bucket with FIFO-fair async acquisition."""

    __slots__ = ("_capacity", "_rate", "_tokens", "_updated", "_lock", "_min_interval", "_last_grant")

    def __init__(self, *, rate: float, capacity: float) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._min_interval = 0.0
        self._last_grant = 0.0

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def apply_min_interval(self, seconds: float) -> None:
        """Honour a robots ``Crawl-delay`` on top of the configured rate."""
        self._min_interval = max(self._min_interval, max(seconds, 0.0))

    async def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> float:
        """Wait for ``tokens`` and return the time spent waiting, in seconds.

        Raises :class:`TimeoutError` if the wait would exceed ``timeout``.
        """
        if tokens <= 0:
            return 0.0
        if tokens > self._capacity:
            raise ValueError(f"cannot acquire {tokens} tokens from a bucket of capacity {self._capacity}")

        started = time.monotonic()
        # The lock is held across the sleep: callers are therefore served in
        # arrival order and no thundering herd forms behind an empty bucket.
        async with self._lock:
            while True:
                self._refill()
                spacing_wait = 0.0
                if self._min_interval > 0.0 and self._last_grant:
                    spacing_wait = max(0.0, self._min_interval - (time.monotonic() - self._last_grant))
                token_wait = 0.0 if self._tokens >= tokens else (tokens - self._tokens) / self._rate
                wait = max(spacing_wait, token_wait)

                if wait <= 0.0:
                    self._tokens -= tokens
                    self._last_grant = time.monotonic()
                    return self._last_grant - started

                if timeout is not None and (time.monotonic() - started) + wait > timeout:
                    raise TimeoutError(
                        f"token bucket wait {wait:.2f}s exceeds timeout {timeout:.2f}s"
                    )
                await asyncio.sleep(min(wait, 1.0))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = now


class RateLimiterRegistry:
    """Lazily-created token bucket per host, with per-host overrides."""

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    def policy_for(self, host: str) -> HostRatePolicy:
        return self._config.host_overrides.get(host, self._config.default_rate)

    async def bucket_for(self, host: str) -> TokenBucket:
        bucket = self._buckets.get(host)
        if bucket is not None:
            return bucket
        async with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                policy = self.policy_for(host)
                bucket = TokenBucket(rate=policy.requests_per_second, capacity=policy.burst)
                if policy.min_crawl_delay_s:
                    bucket.apply_min_interval(policy.min_crawl_delay_s)
                self._buckets[host] = bucket
            return bucket

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            host: {"tokens": round(bucket.tokens(), 3), "rate": bucket.rate, "capacity": bucket.capacity}
            for host, bucket in self._buckets.items()
        }


# --------------------------------------------------------------------------- #
# Bot-challenge detection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BotChallengeSignal:
    kind: str
    evidence: str
    confidence: float
    status_code: int | None = None


class BotChallengeDetector:
    """Heuristics that identify an anti-bot interstitial or WAF block.

    Deliberately biased towards false positives: halting a collector and paging
    an operator is cheap, whereas hammering a host that is actively challenging
    us is a compliance incident.
    """

    _BODY_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], float], ...]] = (
        ("cloudflare", re.compile(r"(cf-chl|cf_chl_opt|/cdn-cgi/challenge-platform|just a moment)", re.I), 0.97),
        ("recaptcha", re.compile(r"(g-recaptcha|recaptcha/api\.js|grecaptcha)", re.I), 0.95),
        ("hcaptcha", re.compile(r"(h-captcha|hcaptcha\.com)", re.I), 0.95),
        ("datadome", re.compile(r"(datadome|dd_?cookie|geo\.captcha-delivery\.com)", re.I), 0.95),
        ("perimeterx", re.compile(r"(px-captcha|_px[cC]aptcha|perimeterx)", re.I), 0.95),
        ("imperva", re.compile(r"(incapsula|_incap_ses|imperva)", re.I), 0.9),
        ("akamai", re.compile(r"(ak_bmsc|akamai bot manager|reference\s*#\d{2}\.\w+)", re.I), 0.85),
        ("generic_captcha", re.compile(r"(captcha|are you (a )?(human|robot)|verify you are human)", re.I), 0.8),
        ("generic_block", re.compile(r"(unusual traffic|automated queries|access denied|bot detected)", re.I), 0.75),
    )

    _HEADER_MARKERS: Final[tuple[tuple[str, str, float], ...]] = (
        ("cloudflare", "cf-mitigated", 0.98),
        ("datadome", "x-datadome", 0.95),
        ("perimeterx", "x-px", 0.9),
        ("imperva", "x-iinfo", 0.85),
        ("akamai", "x-akamai-bot", 0.85),
    )

    _HARD_STATUS: Final[frozenset[int]] = frozenset({401, 403, 407, 451})

    @classmethod
    def detect(
        cls,
        *,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        body_snippet: str | None = None,
        halt_on_http_429: bool = False,
    ) -> BotChallengeSignal | None:
        normalized_headers = {k.lower(): v for k, v in (headers or {}).items()}

        for vendor, marker, confidence in cls._HEADER_MARKERS:
            if marker in normalized_headers:
                return BotChallengeSignal(vendor, f"header:{marker}={normalized_headers[marker]}", confidence, status_code)

        body = (body_snippet or "")[:_BODY_SNIPPET_LIMIT]
        if body:
            for vendor, pattern, confidence in cls._BODY_PATTERNS:
                match = pattern.search(body)
                if match:
                    return BotChallengeSignal(vendor, f"body:{match.group(0)[:120]}", confidence, status_code)

        if status_code in cls._HARD_STATUS:
            return BotChallengeSignal("http_block", f"status:{status_code}", 0.7, status_code)
        if status_code == 429 and halt_on_http_429:
            return BotChallengeSignal("http_throttle", "status:429", 0.6, status_code)
        return None


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    HALTED = "halted"


class BreakerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    state: BreakerState
    consecutive_failures: int
    consecutive_successes: int
    opened_at: datetime | None = None
    halted_at: datetime | None = None
    halt_reason: str | None = None
    total_failures: int = 0
    total_successes: int = 0
    total_trips: int = 0


class CircuitBreaker:
    """Per-source breaker with a terminal, operator-only ``HALTED`` state."""

    def __init__(self, source_id: str, config: BreakerConfig) -> None:
        self._source_id = source_id
        self._config = config
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_monotonic: float | None = None
        self._opened_at: datetime | None = None
        self._halted_at: datetime | None = None
        self._halt_reason: str | None = None
        self._half_open_calls = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_trips = 0
        self._lock = asyncio.Lock()

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def is_halted(self) -> bool:
        return self._state is BreakerState.HALTED

    async def check(self) -> None:
        """Raise if the breaker currently forbids an outbound call."""
        async with self._lock:
            if self._state is BreakerState.HALTED:
                raise SourceHalted(
                    f"source {self._source_id!r} halted: {self._halt_reason}; operator resume required",
                    source_id=self._source_id,
                )

            if self._state is BreakerState.OPEN:
                elapsed = time.monotonic() - (self._opened_monotonic or 0.0)
                remaining = self._config.recovery_timeout_s - elapsed
                if remaining > 0:
                    raise CircuitOpenError(
                        f"circuit open for {self._source_id!r}; retry in {remaining:.1f}s",
                        source_id=self._source_id,
                        retry_after_s=remaining,
                    )
                self._state = BreakerState.HALF_OPEN
                self._half_open_calls = 0
                self._successes = 0
                logger.info("breaker half-open source=%s", self._source_id)

            if self._state is BreakerState.HALF_OPEN:
                if self._half_open_calls >= self._config.half_open_max_calls:
                    raise CircuitOpenError(
                        f"circuit half-open probe limit reached for {self._source_id!r}",
                        source_id=self._source_id,
                        retry_after_s=self._config.recovery_timeout_s,
                    )
                self._half_open_calls += 1

    async def record_success(self) -> None:
        async with self._lock:
            if self._state is BreakerState.HALTED:
                return
            self._total_successes += 1
            self._failures = 0
            if self._state is BreakerState.HALF_OPEN:
                self._successes += 1
                if self._successes >= self._config.success_threshold:
                    self._close_locked()
            else:
                self._state = BreakerState.CLOSED

    async def record_failure(self, reason: str = "") -> None:
        async with self._lock:
            if self._state is BreakerState.HALTED:
                return
            self._total_failures += 1
            self._successes = 0
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN or self._failures >= self._config.failure_threshold:
                self._trip_locked(reason)

    async def halt(self, signal: BotChallengeSignal) -> None:
        """Latch the breaker permanently after a confirmed bot challenge."""
        async with self._lock:
            if self._state is BreakerState.HALTED:
                return
            self._state = BreakerState.HALTED
            self._halted_at = datetime.now(_UTC)
            self._halt_reason = f"{signal.kind} ({signal.evidence}, confidence={signal.confidence:.2f})"
            self._total_trips += 1
            logger.error(
                "HALTED source=%s reason=%s status=%s - collection stopped pending operator review",
                self._source_id,
                self._halt_reason,
                signal.status_code,
            )

    async def resume(self, *, force: bool = False) -> bool:
        """Operator action: clear the breaker.  ``force`` is required to unhalt."""
        async with self._lock:
            if self._state is BreakerState.HALTED and not force:
                return False
            self._close_locked()
            self._halted_at = None
            self._halt_reason = None
            logger.warning("breaker manually resumed source=%s force=%s", self._source_id, force)
            return True

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            source_id=self._source_id,
            state=self._state,
            consecutive_failures=self._failures,
            consecutive_successes=self._successes,
            opened_at=self._opened_at,
            halted_at=self._halted_at,
            halt_reason=self._halt_reason,
            total_failures=self._total_failures,
            total_successes=self._total_successes,
            total_trips=self._total_trips,
        )

    # -- lock-held helpers -------------------------------------------------- #
    def _trip_locked(self, reason: str) -> None:
        self._state = BreakerState.OPEN
        self._opened_monotonic = time.monotonic()
        self._opened_at = datetime.now(_UTC)
        self._half_open_calls = 0
        self._total_trips += 1
        logger.warning(
            "breaker opened source=%s failures=%d reason=%s", self._source_id, self._failures, reason or "n/a"
        )

    def _close_locked(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._half_open_calls = 0
        self._opened_monotonic = None
        self._opened_at = None


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class PolicyGrant(BaseModel):
    """Positive authorisation receipt; attach to lineage for audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    url: str
    host: str
    decision: PolicyDecision = PolicyDecision.ALLOW
    waited_ms: int = Field(ge=0)
    crawl_delay_s: float | None = None
    robots_reason: str = "not_checked"
    granted_at: datetime


class PolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    user_agent: str
    breakers: tuple[BreakerSnapshot, ...]
    buckets: Mapping[str, Mapping[str, float]]
    halted_sources: tuple[str, ...]


class PolicyEngine:
    """Composed robots + rate-limit + circuit-breaker gate for all collectors."""

    def __init__(
        self,
        config: PolicyConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or PolicyConfig()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": self._config.user_agent},
            timeout=httpx.Timeout(self._config.robots_timeout_s),
        )
        self._robots = RobotsCache(
            client=self._client,
            user_agent=self._config.user_agent,
            ttl_s=self._config.robots_ttl_s,
            error_ttl_s=self._config.robots_error_ttl_s,
            timeout_s=self._config.robots_timeout_s,
            fail_closed=self._config.fail_closed,
        )
        self._limiter = RateLimiterRegistry(self._config)
        self._breakers: dict[str, CircuitBreaker] = {}
        self._breaker_lock = asyncio.Lock()

    @property
    def config(self) -> PolicyConfig:
        return self._config

    # -- lifecycle ---------------------------------------------------------- #
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    # -- PolicyGuard protocol ----------------------------------------------- #
    async def authorize(self, *, source_id: str, url: str, tokens: int = 1) -> PolicyGrant:
        """Gate one outbound request.  Raises :class:`PolicyViolation` on refusal."""
        if not self._config.enabled:
            raise PolicyViolation("collection globally disabled by kill switch", source_id=source_id, url=url)
        if source_id in self._config.disabled_sources:
            error = PolicyViolation(f"source {source_id!r} is disabled by configuration", source_id=source_id, url=url)
            error.decision = PolicyDecision.DENY_SOURCE_DISABLED
            raise error

        breaker = await self.breaker_for(source_id)
        await breaker.check()

        host = urlsplit(url).netloc
        if not host:
            raise ValueError(f"policy URL must be absolute: {url!r}")

        verdict = RobotsVerdict(True, "robots_check_disabled")
        if self._config.respect_robots:
            verdict = await self._robots.evaluate(url)
            if not verdict.allowed:
                error_cls = (
                    RobotsUnreachable if verdict.reason.startswith("robots_unreachable") else RobotsDisallowed
                )
                raise error_cls(
                    f"robots.txt forbids {url!r} for {self._config.user_agent!r} ({verdict.reason})",
                    source_id=source_id,
                    url=url,
                )

        bucket = await self._limiter.bucket_for(host)
        if verdict.crawl_delay_s:
            policy = self._limiter.policy_for(host)
            bucket.apply_min_interval(min(verdict.crawl_delay_s, policy.max_crawl_delay_s))

        try:
            waited = await bucket.acquire(float(tokens), timeout=self._config.acquire_timeout_s)
        except TimeoutError as exc:
            raise RateLimitTimeout(
                f"rate limit wait exceeded for host {host!r}: {exc}",
                source_id=source_id,
                url=url,
                retry_after_s=self._config.acquire_timeout_s,
            ) from exc

        return PolicyGrant(
            source_id=source_id,
            url=url,
            host=host,
            waited_ms=int(waited * 1000),
            crawl_delay_s=verdict.crawl_delay_s,
            robots_reason=verdict.reason,
            granted_at=datetime.now(_UTC),
        )

    async def observe_response(
        self,
        *,
        source_id: str,
        url: str,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        body_snippet: str | None = None,
    ) -> None:
        """Feed a response back into the breaker; halts on a bot challenge."""
        breaker = await self.breaker_for(source_id)
        signal = BotChallengeDetector.detect(
            status_code=status_code,
            headers=headers,
            body_snippet=body_snippet,
            halt_on_http_429=self._config.breaker.halt_on_http_429,
        )
        if signal is not None:
            await breaker.halt(signal)
            return
        if status_code >= 500 or status_code == 429:
            await breaker.record_failure(f"http_{status_code}")
            return
        await breaker.record_success()

    async def observe_exception(self, *, source_id: str, url: str, error: BaseException) -> None:
        breaker = await self.breaker_for(source_id)
        await breaker.record_failure(f"{type(error).__name__}: {error}")

    # -- operations --------------------------------------------------------- #
    async def breaker_for(self, source_id: str) -> CircuitBreaker:
        breaker = self._breakers.get(source_id)
        if breaker is not None:
            return breaker
        async with self._breaker_lock:
            breaker = self._breakers.get(source_id)
            if breaker is None:
                breaker = CircuitBreaker(source_id, self._config.breaker)
                self._breakers[source_id] = breaker
            return breaker

    async def resume(self, source_id: str, *, force: bool = False) -> bool:
        """Operator endpoint: clear a tripped (``force=True``: halted) source."""
        breaker = await self.breaker_for(source_id)
        return await breaker.resume(force=force)

    def halted_sources(self) -> tuple[str, ...]:
        return tuple(sorted(sid for sid, b in self._breakers.items() if b.is_halted))

    def invalidate_robots(self, url_or_origin: str) -> None:
        self._robots.invalidate(url_or_origin)

    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(
            enabled=self._config.enabled,
            user_agent=self._config.user_agent,
            breakers=tuple(b.snapshot() for b in self._breakers.values()),
            buckets=self._limiter.snapshot(),
            halted_sources=self.halted_sources(),
        )

    def describe(self) -> dict[str, Any]:  # pragma: no cover - admin surface
        return self.snapshot().model_dump(mode="json")


def build_policy_engine(
    *,
    config: PolicyConfig | None = None,
    disabled_sources: Iterable[str] = (),
) -> PolicyEngine:
    """FastAPI dependency factory - one engine per process, injected per request."""
    base = config or PolicyConfig()
    if disabled_sources:
        base = base.model_copy(update={"disabled_sources": frozenset(disabled_sources)})
    return PolicyEngine(base)
