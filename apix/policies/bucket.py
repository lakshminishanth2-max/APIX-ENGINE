"""Distributed token-bucket rate limiter backed by Redis.

Why Redis and not a per-process bucket
--------------------------------------
The collection tier scales horizontally: several Celery workers, each with
several child processes, may hold tasks for the same source at the same instant.
A per-process bucket would multiply the agreed request rate by the worker count
- which is precisely the failure mode that gets a statistical agency blocked and
is indefensible when the rate was agreed with the source in writing.  State must
therefore live outside the process.

Atomicity
---------
Refill-check-consume is executed as a single Lua script inside Redis, so it is
atomic across the whole fleet: there is no read-modify-write window in which two
workers can both observe "one token left" and both take it.

Fairness and back-pressure
--------------------------
When a caller cannot be served immediately the script returns the exact wait in
seconds; the client sleeps that long and retries rather than spinning.  A
``Crawl-delay`` directive is honoured through ``min_interval``, which enforces
spacing between consecutive grants independently of the bucket's refill rate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Final

import redis
from django.conf import settings

__all__ = ["BucketGrant", "RateLimitTimeout", "RedisTokenBucket", "get_token_bucket"]

logger = logging.getLogger(__name__)

_KEY_PREFIX: Final[str] = "apix:ratelimit:"
_KEY_TTL_SECONDS: Final[int] = 3_600
_MAX_SLEEP: Final[float] = 5.0

# ARGV: rate, capacity, now, requested, min_interval, ttl
# Returns {granted (0|1), payload} where payload is the remaining tokens on a
# grant, or the required wait in seconds on a refusal.
_LUA_ACQUIRE: Final[str] = """
local key          = KEYS[1]
local rate         = tonumber(ARGV[1])
local capacity     = tonumber(ARGV[2])
local now          = tonumber(ARGV[3])
local requested    = tonumber(ARGV[4])
local min_interval = tonumber(ARGV[5])
local ttl          = tonumber(ARGV[6])

local state  = redis.call('HMGET', key, 'tokens', 'ts', 'last')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])
local last   = tonumber(state[3])

if tokens == nil then
    tokens = capacity
    ts = now
    last = 0
end
if last == nil then last = 0 end

-- Refill for elapsed wall time, capped at the bucket capacity.
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local wait = 0.0

-- Crawl-delay style spacing between successive grants.
if min_interval > 0 and last > 0 then
    local spacing = min_interval - (now - last)
    if spacing > wait then wait = spacing end
end

-- Not enough tokens: how long until there are?
if tokens < requested then
    local needed = (requested - tokens) / rate
    if needed > wait then wait = needed end
end

if wait > 0 then
    redis.call('HSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, ttl)
    return {0, tostring(wait)}
end

tokens = tokens - requested
redis.call('HSET', key, 'tokens', tokens, 'ts', now, 'last', now)
redis.call('EXPIRE', key, ttl)
return {1, tostring(tokens)}
"""

_LUA_INSPECT: Final[str] = """
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local state    = redis.call('HMGET', key, 'tokens', 'ts', 'last')
local tokens   = tonumber(state[1])
local ts       = tonumber(state[2])
if tokens == nil then return {tostring(capacity), '0'} end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
return {tostring(tokens), state[3] or '0'}
"""


class RateLimitTimeout(RuntimeError):
    """The caller would have had to wait longer than the allowed budget."""

    policy_blocked: bool = True

    def __init__(self, source_code: str, waited: float, budget: float) -> None:
        super().__init__(
            f"rate limit for {source_code!r} not satisfied within {budget:.1f}s "
            f"(waited {waited:.1f}s)"
        )
        self.source_code = source_code
        self.waited = waited
        self.budget = budget


@dataclass(frozen=True, slots=True)
class BucketGrant:
    """Receipt for one admitted request."""

    source_code: str
    waited_seconds: float
    tokens_remaining: float

    @property
    def waited_ms(self) -> int:
        return int(self.waited_seconds * 1000)


class RedisTokenBucket:
    """Fleet-wide token bucket.  One bucket per ``Source.code``."""

    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client = client or redis.Redis.from_url(
            settings.REDIS_RATELIMIT_URL, decode_responses=True, socket_timeout=5
        )
        self._acquire_script = self._client.register_script(_LUA_ACQUIRE)
        self._inspect_script = self._client.register_script(_LUA_INSPECT)

    @staticmethod
    def key_for(source_code: str) -> str:
        return f"{_KEY_PREFIX}{source_code}"

    def acquire(
        self,
        source_code: str,
        *,
        rate_per_second: float,
        capacity: float,
        tokens: float = 1.0,
        min_interval: float = 0.0,
        timeout: float | None = None,
    ) -> BucketGrant:
        """Block until a token is available; return how long that took.

        Raises :class:`RateLimitTimeout` if the total wait would exceed
        ``timeout``.  Redis being unreachable is treated as a *refusal*, never
        as permission - an unmetered collector is worse than a stalled one.
        """
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("rate_per_second and capacity must be positive")
        if tokens > capacity:
            raise ValueError(f"cannot request {tokens} tokens from a bucket of capacity {capacity}")

        budget = timeout if timeout is not None else float(settings.APIX["RATE_LIMIT_WAIT_TIMEOUT"])
        key = self.key_for(source_code)
        started = time.monotonic()

        while True:
            try:
                granted, payload = self._acquire_script(
                    keys=[key],
                    args=[rate_per_second, capacity, time.time(), tokens, min_interval, _KEY_TTL_SECONDS],
                )
            except redis.RedisError as exc:
                logger.error(
                    "rate limiter unavailable - refusing the request (fail closed)",
                    extra={"source": source_code, "error": str(exc)},
                )
                raise RateLimitTimeout(source_code, time.monotonic() - started, budget) from exc

            if int(granted) == 1:
                return BucketGrant(
                    source_code=source_code,
                    waited_seconds=time.monotonic() - started,
                    tokens_remaining=float(payload),
                )

            wait = float(payload)
            elapsed = time.monotonic() - started
            if elapsed + wait > budget:
                raise RateLimitTimeout(source_code, elapsed, budget)
            time.sleep(min(wait, _MAX_SLEEP))

    def try_acquire(self, source_code: str, **kwargs: Any) -> BucketGrant | None:
        """Non-blocking variant: returns ``None`` instead of waiting."""
        try:
            return self.acquire(source_code, timeout=0.0, **kwargs)
        except RateLimitTimeout:
            return None

    def inspect(self, source_code: str, *, rate_per_second: float, capacity: float) -> dict[str, float]:
        """Current bucket level - powers the compliance panel in the admin."""
        try:
            tokens, last = self._inspect_script(
                keys=[self.key_for(source_code)], args=[rate_per_second, capacity, time.time()]
            )
            return {
                "tokens": float(tokens),
                "capacity": float(capacity),
                "rate_per_second": float(rate_per_second),
                "last_grant_epoch": float(last or 0.0),
            }
        except redis.RedisError as exc:  # pragma: no cover - diagnostics path
            logger.warning("bucket inspect failed", extra={"source": source_code, "error": str(exc)})
            return {"tokens": 0.0, "capacity": float(capacity), "rate_per_second": float(rate_per_second),
                    "last_grant_epoch": 0.0}

    def reset(self, source_code: str) -> None:
        """Operator action: drop the bucket so a source starts clean."""
        self._client.delete(self.key_for(source_code))


_bucket: RedisTokenBucket | None = None


def get_token_bucket() -> RedisTokenBucket:
    """Process-wide singleton (Redis connections are pooled internally)."""
    global _bucket
    if _bucket is None:
        _bucket = RedisTokenBucket()
    return _bucket
