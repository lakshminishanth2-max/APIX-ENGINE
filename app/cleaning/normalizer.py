"""Field normalisation and the "Missing != Zero" component auditor.

Two independent concerns live here:

``parse_money``
    A tolerant ``Decimal`` parser for the money strings airline and OTA pages
    actually emit: ``"Rs. 4,567"``, ``"INR 4.567,00"``, ``"₹ 12 345.50"``,
    ``"(1,234.56)"``, ``"1.2k"``, ``"3.4 lakh"``, ``"--"``.  It never falls back
    to ``float`` - every value is exact ``Decimal`` arithmetic - and it reports
    *why* a value is absent instead of silently returning zero.

``ComponentAuditor``
    Enforces the platform's cardinal cleaning rule: **a missing fare component
    is not a zero fare component.**  A source that publishes only an all-in
    price has *unknown* taxes; a source that waives a convenience fee has taxes
    of exactly ``Decimal("0.00")``.  Collapsing the two corrupts every
    downstream elementary index, so the auditor keeps them in distinct states,
    refuses to impute across the boundary, and marks a record
    ``index_eligible=False`` rather than guessing.

The module has no database, network or framework dependency; it shares only the
platform vocabulary enums so cleaning can run in a worker, a notebook or a test.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from app.collectors.base import CabinClass

__all__ = [
    "AuditFlag",
    "ComponentAudit",
    "ComponentAuditor",
    "ComponentAuditReport",
    "ComponentState",
    "FareNormalizer",
    "MoneyParseResult",
    "NormalizationIssue",
    "NormalizedFare",
    "ParseStatus",
    "parse_money",
]

_UTC: Final = timezone.utc
_CENT: Final[Decimal] = Decimal("0.01")
_ZERO: Final[Decimal] = Decimal("0")

# --------------------------------------------------------------------------- #
# Money parsing
# --------------------------------------------------------------------------- #
_MISSING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "–",
        "—",
        "n/a",
        "na",
        "n.a.",
        "nil",
        "none",
        "null",
        "not available",
        "unavailable",
        "tba",
        "tbd",
        "sold out",
        "no fare",
        "call for price",
        "?",
    }
)

_CURRENCY_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "₹": "INR",
        "rs": "INR",
        "rs.": "INR",
        "inr": "INR",
        "₨": "INR",
        "$": "USD",
        "us$": "USD",
        "usd": "USD",
        "€": "EUR",
        "eur": "EUR",
        "£": "GBP",
        "gbp": "GBP",
        "¥": "JPY",
        "jpy": "JPY",
        "aed": "AED",
        "د.إ": "AED",
        "sgd": "SGD",
        "s$": "SGD",
        "thb": "THB",
        "฿": "THB",
    }
)

#: Magnitude suffixes, including the Indian numbering system.
_MULTIPLIERS: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        "k": Decimal("1000"),
        "thousand": Decimal("1000"),
        "m": Decimal("1000000"),
        "mn": Decimal("1000000"),
        "million": Decimal("1000000"),
        "l": Decimal("100000"),
        "lac": Decimal("100000"),
        "lakh": Decimal("100000"),
        "lakhs": Decimal("100000"),
        "cr": Decimal("10000000"),
        "crore": Decimal("10000000"),
        "crores": Decimal("10000000"),
    }
)

#: Optional sign, currency prefix, the numeric body, then a magnitude/code suffix.
_MONEY_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^
    (?P<sign>[-+])?                 # leading sign (U+2212 folded in _strip_layout)
    (?P<prefix>[^\d\s]{0,6}?)       # currency symbol or code
    \s*
    (?P<number>\d[\d.,]*)           # grouped and/or decimal numeric body
    \s*
    (?P<suffix>[^\d\s]{0,8})        # magnitude suffix and/or currency code
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

_NON_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"[^\d.,]")


class ParseStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"
    UNPARSEABLE = "unparseable"
    NEGATIVE_REJECTED = "negative_rejected"


@dataclass(frozen=True, slots=True)
class MoneyParseResult:
    """Outcome of parsing one money string; ``value is None`` means unknown."""

    value: Decimal | None
    status: ParseStatus
    raw: str | None
    currency: str | None = None
    note: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status is ParseStatus.OK and self.value is not None

    def unwrap_or_none(self) -> Decimal | None:
        return self.value if self.is_ok else None


def _strip_layout(raw: str) -> str:
    """NFKC-fold, drop every flavour of space, and normalise dashes."""
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    for space in (" ", " ", " ", " ", " "):
        text = text.replace(space, " ")
    return " ".join(text.split()).strip()


def _resolve_separators(number: str) -> str:
    """Collapse grouping separators and force ``.`` as the decimal mark.

    Handles ``1,234.56`` (US), ``1.234,56`` (EU), ``1,23,456.78`` (Indian
    grouping) and the ambiguous single-separator cases.
    """
    body = _NON_NUMERIC_RE.sub("", number)
    last_dot = body.rfind(".")
    last_comma = body.rfind(",")

    if last_dot >= 0 and last_comma >= 0:
        # Whichever separator comes last is the decimal mark; the other groups.
        decimal_pos = max(last_dot, last_comma)
        integer_part = body[:decimal_pos].replace(",", "").replace(".", "")
        fraction = body[decimal_pos + 1 :]
        return f"{integer_part or '0'}.{fraction}"

    mark = "." if last_dot >= 0 else ("," if last_comma >= 0 else "")
    if not mark:
        return body

    head, _, tail = body.rpartition(mark)
    if body.count(mark) > 1:
        # Repeated separator can only be grouping: 1.234.567 / 1,23,456
        return body.replace(mark, "")
    if len(tail) == 3 and head:
        # Ambiguous "4,500" / "4.500": three trailing digits means grouping.
        return head + tail
    if len(tail) in (1, 2) or not head:
        return f"{head or '0'}.{tail}"
    return body.replace(mark, "")


def parse_money(
    raw: Any,
    *,
    default_currency: str | None = None,
    allow_negative: bool = False,
    quantize: bool = True,
) -> MoneyParseResult:
    """Parse a money-ish value into an exact ``Decimal``.

    Returns :class:`MoneyParseResult` - never raises, never substitutes zero.
    """
    if raw is None:
        return MoneyParseResult(None, ParseStatus.MISSING, None, default_currency, "null input")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        value = Decimal(raw)
    elif isinstance(raw, float):
        # Cross the float boundary exactly once, via str, to avoid binary dust.
        value = Decimal(str(raw))
    elif isinstance(raw, str):
        return _parse_money_string(
            raw,
            default_currency=default_currency,
            allow_negative=allow_negative,
            quantize=quantize,
        )
    else:
        return MoneyParseResult(None, ParseStatus.UNPARSEABLE, str(raw), default_currency, f"unsupported type {type(raw).__name__}")

    if value < _ZERO and not allow_negative:
        return MoneyParseResult(None, ParseStatus.NEGATIVE_REJECTED, str(raw), default_currency, "negative amount")
    return MoneyParseResult(
        value.quantize(_CENT, rounding=ROUND_HALF_UP) if quantize else value,
        ParseStatus.OK,
        str(raw),
        default_currency,
    )


def _parse_money_string(
    raw: str,
    *,
    default_currency: str | None,
    allow_negative: bool,
    quantize: bool,
) -> MoneyParseResult:
    text = _strip_layout(raw)
    if text.lower() in _MISSING_TOKENS:
        return MoneyParseResult(None, ParseStatus.MISSING, raw, default_currency, "explicit missing token")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    lowered = text.lower()
    currency = default_currency
    for token, code in _CURRENCY_SYMBOLS.items():
        if lowered.startswith(token) or lowered.endswith(token):
            currency = code
            break

    match = _MONEY_RE.match(text.replace(" ", ""))
    if match is None:
        digits = re.sub(r"[^\d.,\-]", "", text)
        if not re.search(r"\d", digits):
            return MoneyParseResult(None, ParseStatus.MISSING, raw, currency, "no digits present")
        match = _MONEY_RE.match(digits)
        if match is None:
            return MoneyParseResult(None, ParseStatus.UNPARSEABLE, raw, currency, "no numeric pattern matched")

    if match.group("sign") == "-":
        negative = True

    try:
        value = Decimal(_resolve_separators(match.group("number")))
    except (InvalidOperation, ValueError):
        return MoneyParseResult(None, ParseStatus.UNPARSEABLE, raw, currency, "decimal conversion failed")

    suffix = (match.group("suffix") or "").strip(". ").lower()
    if suffix in _MULTIPLIERS:
        value *= _MULTIPLIERS[suffix]
    elif suffix and suffix.upper() in _CURRENCY_SYMBOLS.values():
        currency = suffix.upper()
    elif suffix and suffix not in _CURRENCY_SYMBOLS:
        return MoneyParseResult(None, ParseStatus.UNPARSEABLE, raw, currency, f"unknown suffix {suffix!r}")

    if negative:
        value = -value
    if value < _ZERO and not allow_negative:
        return MoneyParseResult(None, ParseStatus.NEGATIVE_REJECTED, raw, currency, "negative amount rejected")

    return MoneyParseResult(
        value.quantize(_CENT, rounding=ROUND_HALF_UP) if quantize else value,
        ParseStatus.OK,
        raw,
        currency,
    )


# --------------------------------------------------------------------------- #
# Missing != Zero
# --------------------------------------------------------------------------- #
class ComponentState(StrEnum):
    """Why a component holds the value it holds.  Never collapse these."""

    PRESENT = "present"
    #: Published by the source and genuinely equal to zero (waived / included).
    EXPLICIT_ZERO = "explicit_zero"
    #: The source did not publish this component at all - value is UNKNOWN.
    MISSING = "missing"
    #: The source published something we could not interpret.
    UNPARSEABLE = "unparseable"
    #: Derived by the auditor from the total and the other known components.
    DERIVED = "derived"

    @property
    def is_known(self) -> bool:
        return self in (ComponentState.PRESENT, ComponentState.EXPLICIT_ZERO, ComponentState.DERIVED)


class AuditFlag(StrEnum):
    COMPONENTS_COMPLETE = "components_complete"
    COMPONENT_MISSING = "component_missing"
    COMPONENT_UNPARSEABLE = "component_unparseable"
    EXPLICIT_ZERO_PRESENT = "explicit_zero_present"
    TOTAL_MISSING = "total_missing"
    TOTAL_MISMATCH = "total_mismatch"
    TOTAL_DERIVED = "total_derived"
    COMPONENT_DERIVED = "component_derived"
    NEGATIVE_VALUE = "negative_value"
    ALL_IN_PRICE_ONLY = "all_in_price_only"


class ComponentAudit(BaseModel):
    """Per-component verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: Decimal | None
    state: ComponentState
    raw: str | None = None
    note: str | None = None


class ComponentAuditReport(BaseModel):
    """Whether a fare's breakdown can be trusted, and exactly why not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    components: tuple[ComponentAudit, ...]
    total: Decimal | None
    total_state: ComponentState
    component_sum: Decimal | None
    residual: Decimal | None
    flags: frozenset[AuditFlag]
    #: True only when every component is known and reconciles with the total.
    breakdown_trusted: bool
    #: True when the record may enter the elementary index (needs a valid total).
    index_eligible: bool
    notes: tuple[str, ...] = ()

    @property
    def missing_components(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.state is ComponentState.MISSING)

    @property
    def zero_components(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.state is ComponentState.EXPLICIT_ZERO)


class ComponentAuditor:
    """Reconciles fare components against the total without ever imputing zero.

    Behaviour:

    * a ``None`` component stays ``MISSING`` - it is *not* rewritten to zero;
    * an explicit ``Decimal("0")`` stays ``EXPLICIT_ZERO`` and counts towards
      the reconciliation sum;
    * exactly one missing component may be **derived** as the residual of the
      total (opt-in, and marked as ``DERIVED`` so lineage stays honest);
    * two or more missing components make the breakdown untrustworthy, though
      the record can still carry the all-in total into the index.
    """

    #: Components that make up the total, in reconciliation order.
    COMPONENT_NAMES: Final[tuple[str, ...]] = ("base_fare", "surcharges", "taxes", "fees")

    def __init__(
        self,
        *,
        absolute_tolerance: Decimal = Decimal("0.05"),
        relative_tolerance: Decimal = Decimal("0.005"),
        derive_single_residual: bool = True,
    ) -> None:
        self._abs_tol = absolute_tolerance
        self._rel_tol = relative_tolerance
        self._derive = derive_single_residual

    def audit(
        self,
        components: Mapping[str, Any],
        total: Any,
        *,
        component_names: tuple[str, ...] | None = None,
    ) -> ComponentAuditReport:
        names = component_names or self.COMPONENT_NAMES
        flags: set[AuditFlag] = set()
        notes: list[str] = []
        audits: list[ComponentAudit] = []

        for name in names:
            parsed = parse_money(components.get(name))
            audits.append(self._classify(name, parsed, flags))

        total_parsed = parse_money(total)
        total_value = total_parsed.unwrap_or_none()
        total_state = self._state_for(total_parsed)
        if total_value is None:
            flags.add(AuditFlag.TOTAL_MISSING)
            notes.append(f"total unusable: {total_parsed.status}")

        known = [a for a in audits if a.state.is_known and a.value is not None]
        missing = [a for a in audits if not a.state.is_known]

        # Optionally recover a single unknown component as the residual.
        if self._derive and len(missing) == 1 and total_value is not None and len(known) == len(names) - 1:
            residual = total_value - sum((a.value for a in known), _ZERO)
            if residual >= _ZERO:
                target = missing[0]
                derived = ComponentAudit(
                    name=target.name,
                    value=residual.quantize(_CENT, rounding=ROUND_HALF_UP),
                    state=ComponentState.DERIVED,
                    raw=target.raw,
                    note="derived as total minus known components",
                )
                audits = [derived if a.name == target.name else a for a in audits]
                known.append(derived)
                missing = []
                flags.add(AuditFlag.COMPONENT_DERIVED)
                notes.append(f"{target.name} derived as residual {derived.value}")

        component_sum: Decimal | None = None
        residual: Decimal | None = None
        if not missing:
            component_sum = sum((a.value for a in known if a.value is not None), _ZERO)
            if total_value is None:
                total_value = component_sum
                total_state = ComponentState.DERIVED
                flags.add(AuditFlag.TOTAL_DERIVED)
                flags.discard(AuditFlag.TOTAL_MISSING)
                notes.append("total derived from complete component breakdown")
            residual = total_value - component_sum
            if self._within_tolerance(residual, total_value):
                flags.add(AuditFlag.COMPONENTS_COMPLETE)
            else:
                flags.add(AuditFlag.TOTAL_MISMATCH)
                notes.append(f"components sum to {component_sum} but total is {total_value} (residual {residual})")
        elif len(missing) == len(names) and total_value is not None:
            flags.add(AuditFlag.ALL_IN_PRICE_ONLY)
            notes.append("source publishes an all-in price only; components are unknown, not zero")

        breakdown_trusted = AuditFlag.COMPONENTS_COMPLETE in flags and AuditFlag.NEGATIVE_VALUE not in flags
        index_eligible = (
            total_value is not None
            and total_value > _ZERO
            and AuditFlag.NEGATIVE_VALUE not in flags
            and AuditFlag.TOTAL_MISMATCH not in flags
        )

        return ComponentAuditReport(
            components=tuple(audits),
            total=total_value,
            total_state=total_state,
            component_sum=component_sum,
            residual=residual,
            flags=frozenset(flags),
            breakdown_trusted=breakdown_trusted,
            index_eligible=index_eligible,
            notes=tuple(notes),
        )

    # -- internals ---------------------------------------------------------- #
    def _classify(self, name: str, parsed: MoneyParseResult, flags: set[AuditFlag]) -> ComponentAudit:
        state = self._state_for(parsed)
        if state is ComponentState.MISSING:
            flags.add(AuditFlag.COMPONENT_MISSING)
        elif state is ComponentState.UNPARSEABLE:
            flags.add(AuditFlag.COMPONENT_UNPARSEABLE)
        elif state is ComponentState.EXPLICIT_ZERO:
            flags.add(AuditFlag.EXPLICIT_ZERO_PRESENT)
        if parsed.status is ParseStatus.NEGATIVE_REJECTED:
            flags.add(AuditFlag.NEGATIVE_VALUE)
        return ComponentAudit(
            name=name,
            value=parsed.unwrap_or_none(),
            state=state,
            raw=parsed.raw,
            note=parsed.note,
        )

    @staticmethod
    def _state_for(parsed: MoneyParseResult) -> ComponentState:
        if parsed.status is ParseStatus.MISSING:
            return ComponentState.MISSING
        if parsed.status is not ParseStatus.OK or parsed.value is None:
            return ComponentState.UNPARSEABLE
        return ComponentState.EXPLICIT_ZERO if parsed.value == _ZERO else ComponentState.PRESENT

    def _within_tolerance(self, residual: Decimal, total: Decimal) -> bool:
        allowed = max(self._abs_tol, (abs(total) * self._rel_tol))
        return abs(residual) <= allowed


# --------------------------------------------------------------------------- #
# Whole-record normalisation
# --------------------------------------------------------------------------- #
_CABIN_SYNONYMS: Final[Mapping[str, CabinClass]] = MappingProxyType(
    {
        "y": CabinClass.ECONOMY,
        "e": CabinClass.ECONOMY,
        "eco": CabinClass.ECONOMY,
        "econ": CabinClass.ECONOMY,
        "economy": CabinClass.ECONOMY,
        "economy class": CabinClass.ECONOMY,
        "coach": CabinClass.ECONOMY,
        "main cabin": CabinClass.ECONOMY,
        "w": CabinClass.PREMIUM_ECONOMY,
        "pe": CabinClass.PREMIUM_ECONOMY,
        "premium": CabinClass.PREMIUM_ECONOMY,
        "premium economy": CabinClass.PREMIUM_ECONOMY,
        "c": CabinClass.BUSINESS,
        "j": CabinClass.BUSINESS,
        "biz": CabinClass.BUSINESS,
        "business": CabinClass.BUSINESS,
        "business class": CabinClass.BUSINESS,
        "f": CabinClass.FIRST,
        "first": CabinClass.FIRST,
        "first class": CabinClass.FIRST,
    }
)

_STATION_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")
_CARRIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9]{2,3}$")
_FLIGHT_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Z0-9]{2,3})\s*-?\s*(\d{1,4})$")


class NormalizationIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    code: str
    detail: str


class NormalizedFare(BaseModel):
    """Canonical, index-ready representation of one observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    carrier_code: str
    flight_number: str | None
    origin: str
    destination: str
    departure_date: date
    departure_time: str | None = None
    observation_ts: datetime
    days_to_departure: int = Field(ge=0)
    cabin: CabinClass
    currency: str

    base_fare: Decimal | None
    surcharges: Decimal | None
    taxes: Decimal | None
    fees: Decimal | None
    total_fare: Decimal = Field(gt=_ZERO)

    audit: ComponentAuditReport
    issues: tuple[NormalizationIssue, ...] = ()
    raw_digest: str | None = None

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    @property
    def index_eligible(self) -> bool:
        return self.audit.index_eligible


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    fare: NormalizedFare | None
    issues: tuple[NormalizationIssue, ...]

    @property
    def ok(self) -> bool:
        return self.fare is not None


class FareNormalizer:
    """Turns a raw, source-shaped mapping into a :class:`NormalizedFare`.

    Accepts a plain mapping (or any object with the same attribute names), so
    the cleaning tier stays decoupled from the collector transport models.
    """

    def __init__(
        self,
        *,
        auditor: ComponentAuditor | None = None,
        default_currency: str = "INR",
    ) -> None:
        self._auditor = auditor or ComponentAuditor()
        self._default_currency = default_currency

    def normalize(self, record: Mapping[str, Any] | Any) -> NormalizationResult:
        data = record if isinstance(record, Mapping) else _as_mapping(record)
        issues: list[NormalizationIssue] = []

        origin = self._station(data.get("origin"), "origin", issues)
        destination = self._station(data.get("destination"), "destination", issues)
        carrier = self._carrier(data.get("carrier_code"), issues)
        cabin = self._cabin(data.get("cabin"), issues)
        currency = self._currency(data.get("currency"), issues)
        departure = _coerce_date(data.get("departure_date"))
        observation = _coerce_datetime(data.get("observation_ts"))

        if departure is None:
            issues.append(NormalizationIssue(field="departure_date", code="unparseable", detail=str(data.get("departure_date"))))
        if observation is None:
            issues.append(NormalizationIssue(field="observation_ts", code="unparseable", detail=str(data.get("observation_ts"))))

        audit = self._auditor.audit(
            {
                "base_fare": data.get("base_fare"),
                "surcharges": data.get("surcharges"),
                "taxes": data.get("taxes"),
                "fees": data.get("fees"),
            },
            data.get("total_fare"),
        )

        if audit.total is None or audit.total <= _ZERO:
            issues.append(NormalizationIssue(field="total_fare", code="unusable_total", detail=str(audit.flags)))

        if None in (origin, destination, carrier, cabin, currency, departure, observation) or audit.total is None:
            return NormalizationResult(None, tuple(issues))

        dtd = data.get("days_to_departure")
        if dtd is None and departure is not None and observation is not None:
            dtd = (departure - observation.astimezone(_UTC).date()).days
        days_to_departure = max(int(dtd or 0), 0)

        by_name = {a.name: a.value for a in audit.components}
        fare = NormalizedFare(
            source_id=str(data.get("source_id") or "unknown"),
            carrier_code=carrier,
            flight_number=self._flight_number(data.get("flight_number"), carrier),
            origin=origin,
            destination=destination,
            departure_date=departure,
            departure_time=self._departure_time(data.get("departure_time")),
            observation_ts=observation,
            days_to_departure=days_to_departure,
            cabin=cabin,
            currency=currency,
            base_fare=by_name.get("base_fare"),
            surcharges=by_name.get("surcharges"),
            taxes=by_name.get("taxes"),
            fees=by_name.get("fees"),
            total_fare=audit.total,
            audit=audit,
            issues=tuple(issues),
            raw_digest=data.get("raw_digest"),
        )
        return NormalizationResult(fare, tuple(issues))

    def normalize_many(self, records: Any) -> tuple[tuple[NormalizedFare, ...], tuple[NormalizationResult, ...]]:
        """Return ``(clean, rejected)`` for a batch."""
        clean: list[NormalizedFare] = []
        rejected: list[NormalizationResult] = []
        for record in records:
            result = self.normalize(record)
            if result.fare is not None:
                clean.append(result.fare)
            else:
                rejected.append(result)
        return tuple(clean), tuple(rejected)

    # -- field helpers ------------------------------------------------------ #
    @staticmethod
    def _station(value: Any, field_name: str, issues: list[NormalizationIssue]) -> str | None:
        text = _strip_layout(str(value or "")).upper().replace(" ", "")
        if _STATION_RE.match(text):
            return text
        issues.append(NormalizationIssue(field=field_name, code="invalid_station", detail=str(value)))
        return None

    @staticmethod
    def _carrier(value: Any, issues: list[NormalizationIssue]) -> str | None:
        text = _strip_layout(str(value or "")).upper().replace(" ", "")
        if _CARRIER_RE.match(text):
            return text
        issues.append(NormalizationIssue(field="carrier_code", code="invalid_carrier", detail=str(value)))
        return None

    @staticmethod
    def _flight_number(value: Any, carrier: str) -> str | None:
        if value is None:
            return None
        text = _strip_layout(str(value)).upper()
        match = _FLIGHT_RE.match(text)
        if match is None:
            return None
        prefix, digits = match.groups()
        return f"{prefix or carrier}{int(digits)}"

    @staticmethod
    def _departure_time(value: Any) -> str | None:
        """Canonicalise a local departure time to ``HH:MM`` (24h)."""
        if value is None:
            return None
        text = _strip_layout(str(value)).upper()
        match = re.match(r"^(\d{1,2})[:.]?(\d{2})\s*(AM|PM)?$", text)
        if match is None:
            return None
        hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
        if meridiem == "PM" and hour < 12:
            hour += 12
        elif meridiem == "AM" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _cabin(value: Any, issues: list[NormalizationIssue]) -> CabinClass | None:
        if isinstance(value, CabinClass):
            return value
        key = _strip_layout(str(value or "economy")).lower()
        cabin = _CABIN_SYNONYMS.get(key)
        if cabin is None:
            issues.append(NormalizationIssue(field="cabin", code="unknown_cabin", detail=str(value)))
        return cabin

    def _currency(self, value: Any, issues: list[NormalizationIssue]) -> str | None:
        text = _strip_layout(str(value or self._default_currency))
        code = _CURRENCY_SYMBOLS.get(text.lower(), text.upper())
        if len(code) == 3 and code.isalpha():
            return code
        issues.append(NormalizationIssue(field="currency", code="invalid_currency", detail=str(value)))
        return None


def _as_mapping(obj: Any) -> Mapping[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {
        name: getattr(obj, name, None)
        for name in (
            "source_id",
            "carrier_code",
            "flight_number",
            "origin",
            "destination",
            "departure_date",
            "departure_time",
            "observation_ts",
            "days_to_departure",
            "cabin",
            "currency",
            "base_fare",
            "surcharges",
            "taxes",
            "fees",
            "total_fare",
            "raw_digest",
        )
    }


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=_UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)
    return None
