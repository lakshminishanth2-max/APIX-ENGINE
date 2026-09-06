"""Anti-bot challenge detection.

The platform's position is that a challenge is a **refusal to be collected**.
When a WAF, CAPTCHA or JavaScript interstitial appears, the correct response is
to stop, record ``BOT_CHALLENGE_DETECTED`` in the audit log, trip the circuit
breaker permanently and page an operator, who can contact the source and
negotiate access.  Solving, replaying or fingerprint-spoofing around a challenge
is out of scope for a government statistical system, and this module exists to
make that stop happen quickly and unambiguously.

The heuristics are deliberately biased toward false positives.  Halting a
collector costs one day of one source's data; hammering a host that is actively
challenging us is a compliance incident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from re import Pattern
from collections.abc import Mapping

__all__ = ["BotChallengeDetected", "BotChallengeDetector", "BotChallengeSignal"]

_SNIPPET_LIMIT: Final[int] = 8_192


@dataclass(frozen=True, slots=True)
class BotChallengeSignal:
    """Evidence that a source is challenging automated access."""

    vendor: str
    evidence: str
    confidence: float
    status_code: int | None = None

    def describe(self) -> str:
        return f"{self.vendor} ({self.evidence}, confidence={self.confidence:.2f})"


class BotChallengeDetected(RuntimeError):
    """Terminal: halts the source until an operator intervenes.

    Carries the duck-typed markers the collector base class reads, so the
    collectors package never has to import the policies package.
    """

    policy_blocked: bool = True
    halted: bool = True

    def __init__(self, signal: BotChallengeSignal, *, source_code: str = "") -> None:
        super().__init__(
            f"bot challenge detected on {source_code or 'source'}: {signal.describe()} - "
            "collection halted; no evasion will be attempted"
        )
        self.signal = signal
        self.source_code = source_code


class BotChallengeDetector:
    """Header, status and body heuristics for the common challenge vendors."""

    _HEADER_MARKERS: Final[tuple[tuple[str, str, float], ...]] = (
        ("cloudflare", "cf-mitigated", 0.98),
        ("datadome", "x-datadome", 0.95),
        ("perimeterx", "x-px", 0.92),
        ("imperva", "x-iinfo", 0.88),
        ("akamai", "x-akamai-bot", 0.88),
    )

    _BODY_PATTERNS: Final[tuple[tuple[str, Pattern[str], float], ...]] = (
        ("cloudflare",
         re.compile(r"(cf-chl|cf_chl_opt|/cdn-cgi/challenge-platform|just a moment)", re.I), 0.97),
        ("recaptcha", re.compile(r"(g-recaptcha|recaptcha/api\.js|grecaptcha)", re.I), 0.95),
        ("hcaptcha", re.compile(r"(h-captcha|hcaptcha\.com)", re.I), 0.95),
        ("datadome", re.compile(r"(datadome|geo\.captcha-delivery\.com)", re.I), 0.95),
        ("perimeterx", re.compile(r"(px-captcha|_px[cC]aptcha|perimeterx)", re.I), 0.94),
        ("imperva", re.compile(r"(incapsula|_incap_ses|imperva)", re.I), 0.90),
        ("akamai", re.compile(r"(ak_bmsc|akamai bot manager|reference\s*#\d{2}\.\w+)", re.I), 0.85),
        ("generic_captcha",
         re.compile(r"(captcha|are you (a )?(human|robot)|verify (that )?you are human)", re.I), 0.80),
        ("generic_block",
         re.compile(r"(unusual traffic|automated (queries|requests)|access denied|bot detected|"
                    r"request blocked)", re.I), 0.75),
    )

    #: Statuses that, on their own, indicate a refusal to serve automation.
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
        """Return a signal when a challenge is recognised, else ``None``.

        ``429`` alone is treated as a *rate* signal rather than a challenge -
        it means slow down, and the token bucket already knows how.  Set
        ``halt_on_http_429`` for a source whose terms make throttling a
        terminating condition.
        """
        normalized = {key.lower(): value for key, value in (headers or {}).items()}

        for vendor, marker, confidence in cls._HEADER_MARKERS:
            if marker in normalized:
                return BotChallengeSignal(
                    vendor, f"header:{marker}={normalized[marker]}"[:200], confidence, status_code
                )

        body = (body_snippet or "")[:_SNIPPET_LIMIT]
        if body:
            for vendor, pattern, confidence in cls._BODY_PATTERNS:
                match = pattern.search(body)
                if match:
                    return BotChallengeSignal(
                        vendor, f"body:{match.group(0)[:120]}", confidence, status_code
                    )

        if status_code in cls._HARD_STATUS:
            return BotChallengeSignal("http_block", f"status:{status_code}", 0.70, status_code)
        if status_code == 429 and halt_on_http_429:
            return BotChallengeSignal("http_throttle", "status:429", 0.60, status_code)
        return None
