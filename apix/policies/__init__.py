"""Ethical collection policy: robots.txt, rate limiting, circuit breaking."""

from __future__ import annotations

from apix.policies.bucket import RateLimitTimeout, RedisTokenBucket, get_token_bucket
from apix.policies.challenge import BotChallengeDetected, BotChallengeDetector, BotChallengeSignal
from apix.policies.engine import (
    CircuitOpen,
    PolicyEngine,
    PolicyGrant,
    PolicyViolation,
    RobotsDisallowed,
    SourceHalted,
    get_policy_engine,
)

__all__ = [
    "BotChallengeDetected",
    "BotChallengeDetector",
    "BotChallengeSignal",
    "CircuitOpen",
    "PolicyEngine",
    "PolicyGrant",
    "PolicyViolation",
    "RateLimitTimeout",
    "RedisTokenBucket",
    "RobotsDisallowed",
    "SourceHalted",
    "get_policy_engine",
    "get_token_bucket",
]
