"""Localized Indian currency parsing into exact ``Decimal``.

Sources emit money in every shape imaginable::

    "₹ 4,521.00"   "INR 4521"      "Rs. 4,521"     "4,521.00"
    "₹4.521,00"    "1.2k"          "3.4 lakh"      "12 345.50"
    "--"           "N/A"           "Sold out"      ""

The parser has one hard rule: **it never invents a number.**  Anything it cannot
confidently read comes back as :attr:`ParseStatus.MISSING` or
:attr:`ParseStatus.UNPARSEABLE` with ``value = None``.  Returning ``Decimal(0)``
for an unreadable price would put a zero into a geometric mean and destroy the
elementary aggregate for that stratum.

``float`` never appears: the string is converted straight to ``Decimal``, and a
Python ``float`` input crosses the boundary exactly once via ``str()`` so no
binary rounding dust enters the statistical path.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final
from re import Pattern
from collections.abc import Mapping

__all__ = [
    "MoneyParseResult",
    "ParseStatus",
    "parse_money",
    "parse_money_or_none",
    "resolve_separators",
]

_CENT: Final[Decimal] = Decimal("0.01")
_ZERO: Final[Decimal] = Decimal("0")

#: Tokens that explicitly mean "no value published".
MISSING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "", "-", "--", "---", "n/a", "na", "n.a.", "nil", "none", "null",
        "not available", "unavailable", "tba", "tbd", "?", "—", "–",
        "sold out", "soldout", "no seats", "no fare", "call for price",
        "on request", "price on request",
    }
)

#: Symbol / code -> ISO-4217.  Domestic APIx is INR-only, but OTAs sometimes
#: quote in USD for the same seat and that must be detected, never silently
#: treated as rupees.
CURRENCY_TOKENS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "₹": "INR", "₨": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
        "$": "USD", "us$": "USD", "usd": "USD",
        "€": "EUR", "eur": "EUR",
        "£": "GBP", "gbp": "GBP",
        "aed": "AED", "sgd": "SGD", "thb": "THB",
    }
)

#: Magnitude suffixes including the Indian numbering system.
MULTIPLIERS: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        "k": Decimal("1000"), "thousand": Decimal("1000"),
        "m": Decimal("1000000"), "mn": Decimal("1000000"), "million": Decimal("1000000"),
        "l": Decimal("100000"), "lac": Decimal("100000"), "lakh": Decimal("100000"),
        "lakhs": Decimal("100000"),
        "cr": Decimal("10000000"), "crore": Decimal("10000000"), "crores": Decimal("10000000"),
    }
)

#: sign, currency prefix, numeric body, magnitude/code suffix.
MONEY_RE: Final[Pattern[str]] = re.compile(
    r"""
    ^
    (?P<sign>[-+])?             # leading sign (U+2212 folded beforehand)
    (?P<prefix>[^\d\s]{0,6}?)   # currency symbol or code
    \s*
    (?P<number>\d[\d.,]*)       # grouped and/or decimal numeric body
    \s*
    (?P<suffix>[^\d\s]{0,8})    # magnitude suffix and/or currency code
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

_NON_NUMERIC_RE: Final[Pattern[str]] = re.compile(r"[^\d.,]")


class ParseStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"
    UNPARSEABLE = "unparseable"
    NEGATIVE_REJECTED = "negative_rejected"
    WRONG_CURRENCY = "wrong_currency"


@dataclass(frozen=True, slots=True)
class MoneyParseResult:
    """``value is None`` always means *unknown*, never *zero*."""

    value: Decimal | None
    status: ParseStatus
    raw: str | None
    currency: str | None = None
    note: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status is ParseStatus.OK and self.value is not None

    @property
    def is_explicit_zero(self) -> bool:
        return self.is_ok and self.value == _ZERO

    def unwrap_or_none(self) -> Decimal | None:
        return self.value if self.is_ok else None


def _fold(raw: str) -> str:
    """NFKC-normalise, unify dashes, and collapse every kind of space."""
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    for space in (" ", " ", " ", " ", " "):
        text = text.replace(space, " ")
    return " ".join(text.split()).strip()


def resolve_separators(number: str) -> str:
    """Collapse grouping separators and force ``.`` as the decimal mark.

    Covers ``1,234.56`` (US), ``1.234,56`` (EU), ``1,23,456.78`` (Indian
    lakh/crore grouping) and the ambiguous single-separator cases.  For a lone
    separator followed by exactly three digits - ``4,500`` or ``4.500`` - the
    grouping reading wins, because Indian fares are quoted in whole rupees far
    more often than to three decimal places.
    """
    body = _NON_NUMERIC_RE.sub("", number)
    last_dot = body.rfind(".")
    last_comma = body.rfind(",")

    if last_dot >= 0 and last_comma >= 0:
        decimal_pos = max(last_dot, last_comma)
        integer_part = body[:decimal_pos].replace(",", "").replace(".", "")
        return f"{integer_part or '0'}.{body[decimal_pos + 1:]}"

    mark = "." if last_dot >= 0 else ("," if last_comma >= 0 else "")
    if not mark:
        return body

    head, _, tail = body.rpartition(mark)
    if body.count(mark) > 1:
        return body.replace(mark, "")           # repeated => grouping
    if len(tail) == 3 and head:
        return head + tail                       # "4,500" => 4500
    if len(tail) in (1, 2) or not head:
        return f"{head or '0'}.{tail}"
    return body.replace(mark, "")


def parse_money(
    raw: Any,
    *,
    expected_currency: str | None = None,
    allow_negative: bool = False,
    quantize: bool = True,
) -> MoneyParseResult:
    """Parse any money-ish value.  Never raises, never substitutes zero."""
    if raw is None:
        return MoneyParseResult(None, ParseStatus.MISSING, None, expected_currency, "key absent")
    if isinstance(raw, bool):
        return MoneyParseResult(None, ParseStatus.UNPARSEABLE, str(raw), expected_currency, "boolean")
    if isinstance(raw, Decimal):
        value, currency = raw, expected_currency
    elif isinstance(raw, int):
        value, currency = Decimal(raw), expected_currency
    elif isinstance(raw, float):
        # The single, deliberate float boundary crossing - via str, never binary.
        value, currency = Decimal(str(raw)), expected_currency
    elif isinstance(raw, str):
        return _parse_string(
            raw,
            expected_currency=expected_currency,
            allow_negative=allow_negative,
            quantize=quantize,
        )
    else:
        return MoneyParseResult(
            None, ParseStatus.UNPARSEABLE, str(raw), expected_currency,
            f"unsupported type {type(raw).__name__}",
        )

    if value < _ZERO and not allow_negative:
        return MoneyParseResult(None, ParseStatus.NEGATIVE_REJECTED, str(raw), currency, "negative amount")
    return MoneyParseResult(
        value.quantize(_CENT, rounding=ROUND_HALF_UP) if quantize else value,
        ParseStatus.OK, str(raw), currency,
    )


def _parse_string(
    raw: str, *, expected_currency: str | None, allow_negative: bool, quantize: bool
) -> MoneyParseResult:
    text = _fold(raw)
    if text.lower() in MISSING_TOKENS:
        return MoneyParseResult(None, ParseStatus.MISSING, raw, expected_currency, "explicit missing token")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()

    lowered = text.lower()
    currency = expected_currency
    for token, code in CURRENCY_TOKENS.items():
        if lowered.startswith(token) or lowered.endswith(token):
            currency = code
            break

    compact = text.replace(" ", "")
    match = MONEY_RE.match(compact)
    if match is None:
        digits = re.sub(r"[^\d.,\-]", "", compact)
        if not re.search(r"\d", digits):
            return MoneyParseResult(None, ParseStatus.MISSING, raw, currency, "no digits present")
        match = MONEY_RE.match(digits)
        if match is None:
            return MoneyParseResult(None, ParseStatus.UNPARSEABLE, raw, currency, "no numeric pattern")

    if match.group("sign") == "-":
        negative = True

    try:
        value = Decimal(resolve_separators(match.group("number")))
    except (InvalidOperation, ValueError):
        return MoneyParseResult(None, ParseStatus.UNPARSEABLE, raw, currency, "decimal conversion failed")

    suffix = (match.group("suffix") or "").strip(". ").lower()
    if suffix in MULTIPLIERS:
        value *= MULTIPLIERS[suffix]
    elif suffix and suffix.upper() in set(CURRENCY_TOKENS.values()):
        currency = suffix.upper()
    elif suffix and suffix not in CURRENCY_TOKENS:
        return MoneyParseResult(None, ParseStatus.UNPARSEABLE, raw, currency, f"unknown suffix {suffix!r}")

    if negative:
        value = -value
    if value < _ZERO and not allow_negative:
        return MoneyParseResult(None, ParseStatus.NEGATIVE_REJECTED, raw, currency, "negative amount")

    if expected_currency and currency and currency != expected_currency:
        return MoneyParseResult(
            None, ParseStatus.WRONG_CURRENCY, raw, currency,
            f"quoted in {currency}, expected {expected_currency}",
        )

    return MoneyParseResult(
        value.quantize(_CENT, rounding=ROUND_HALF_UP) if quantize else value,
        ParseStatus.OK, raw, currency or expected_currency,
    )


def parse_money_or_none(raw: Any, **kwargs: Any) -> Decimal | None:
    """Shorthand for call sites that only need the value."""
    return parse_money(raw, **kwargs).unwrap_or_none()
