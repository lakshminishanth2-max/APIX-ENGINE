"""Cryptographic provenance: canonical serialisation, Merkle roots, hash chains.

A published index level is a regulatory artefact.  Months later somebody will
ask why DEL-BOM T+7 printed 118.4 on a particular day, and the answer has to be
better than "that is what the table says".  Three primitives make the answer
verifiable:

**Canonical serialisation.**  One deterministic byte representation for any
payload: keys sorted, ``Decimal`` rendered as exact text with trailing zeros
normalised so ``100`` and ``100.00`` hash identically, datetimes as UTC
ISO-8601, and ``float`` rejected outright - binary floating point is not
reproducible across platforms and has no place in a monetary lineage.

**Merkle root over inputs.**  The SHA-256 ingress hashes of every
:class:`~apix.models.RawObservation` that fed a computation are folded into a
binary Merkle tree.  Publishing the root proves which payloads were used without
publishing the payloads, and lets any single input be proven a member later.

**Hash chain over outputs.**  Each :class:`~apix.models.IndexValue` seals itself
with a SHA-256 over its level, its inputs root, its methodology and its
predecessor's hash.  Silently restating any historical print therefore breaks
verification of every value after it - which is exactly the property a national
statistical series needs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Final
from uuid import UUID

__all__ = [
    "ChainVerification",
    "ProvenanceSeal",
    "ProvenanceSealer",
    "canonical_json",
    "merkle_root",
    "sha256_hex",
    "verify_series",
]

UTC: Final = timezone.utc
EMPTY_DIGEST: Final[str] = hashlib.sha256(b"").hexdigest()


# --------------------------------------------------------------------------- #
# Canonical serialisation
# --------------------------------------------------------------------------- #
def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        raise TypeError(
            "float is not permitted in a provenance payload; convert to Decimal first"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal in provenance payload: {value}")
        return f'"{format(value.normalize(), "f")}"'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return f'"{moment.astimezone(UTC).isoformat()}"'
    if isinstance(value, date):
        return f'"{value.isoformat()}"'
    if isinstance(value, UUID):
        return f'"{value}"'
    if isinstance(value, Mapping):
        items = ",".join(
            f"{_canonical(str(key))}:{_canonical(item)}"
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        )
        return "{" + items + "}"
    if isinstance(value, (set, frozenset)):
        return "[" + ",".join(sorted(_canonical(item) for item in value)) + "]"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    raise TypeError(f"unsupported type in provenance payload: {type(value).__name__}")


def canonical_json(payload: Any) -> str:
    """Stable, sorted, float-free JSON text used as digest input."""
    return _canonical(payload)


def sha256_hex(payload: Any) -> str:
    """SHA-256 over a payload's canonical form (or raw bytes)."""
    if isinstance(payload, (bytes, bytearray)):
        return hashlib.sha256(bytes(payload)).hexdigest()
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def merkle_root(digests: Sequence[str]) -> str:
    """Binary Merkle root over an ordered list of hex digests.

    An odd node at any level is duplicated (the Bitcoin convention).  Callers
    must pass the digests in a deterministic order - sorted - so the same input
    set always yields the same root.
    """
    if not digests:
        return EMPTY_DIGEST
    level = [bytes.fromhex(digest) for digest in digests]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)
        ]
    return level[0].hex()


# --------------------------------------------------------------------------- #
# Sealing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ProvenanceSeal:
    """The three hashes written onto a published index value."""

    provenance_hash: str
    previous_hash: str
    inputs_root: str
    raw_observation_count: int
    payload: dict[str, Any]

    def as_model_fields(self) -> dict[str, Any]:
        return {
            "provenance_hash": self.provenance_hash,
            "previous_hash": self.previous_hash,
            "inputs_root": self.inputs_root,
            "raw_observation_count": self.raw_observation_count,
        }


class ProvenanceSealer:
    """Seals an index value to its inputs, its methodology and its predecessor."""

    def __init__(self, *, methodology_code: str, methodology_digest: str = "") -> None:
        self.methodology_code = methodology_code
        self.methodology_digest = methodology_digest

    def seal(
        self,
        *,
        index_date: date,
        index_value: Decimal,
        scope: str,
        lead_window_days: int | None,
        base_period: str,
        ingress_hashes: Iterable[str],
        previous_hash: str = "",
        weight_set_digest: str = "",
        diagnostics: Mapping[str, Any] | None = None,
        computed_at: datetime | None = None,
    ) -> ProvenanceSeal:
        """Compute the seal.

        ``ingress_hashes`` are the SHA-256 ingress digests of the raw payloads
        that fed this value; they are sorted and deduplicated before the Merkle
        fold so collection order cannot change the root.
        """
        ordered = sorted(set(ingress_hashes))
        root = merkle_root(ordered)
        moment = computed_at or datetime.now(UTC)

        body: dict[str, Any] = {
            "methodology_code": self.methodology_code,
            "methodology_digest": self.methodology_digest,
            "scope": scope,
            "lead_window_days": lead_window_days,
            "index_date": index_date,
            "index_value": index_value,
            "base_period": base_period,
            "inputs_root": root,
            "input_count": len(ordered),
            "weight_set_digest": weight_set_digest,
            "previous_hash": previous_hash,
            "computed_at": moment,
            "diagnostics": dict(diagnostics or {}),
        }
        return ProvenanceSeal(
            provenance_hash=sha256_hex(body),
            previous_hash=previous_hash,
            inputs_root=root,
            raw_observation_count=len(ordered),
            payload=body,
        )

    @staticmethod
    def recompute(payload: Mapping[str, Any]) -> str:
        """Recompute a hash from a stored ``computation_metadata['seal']`` body."""
        return sha256_hex(dict(payload))


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ChainVerification:
    scope: str
    verified: int
    intact: bool
    broken_at: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "verified": self.verified,
            "intact": self.intact,
            "broken_at": self.broken_at,
            "reason": self.reason,
        }


def verify_series(values: Sequence[Any]) -> ChainVerification:
    """Verify hashes and back-links across an ordered run of ``IndexValue``.

    ``values`` must be ordered by ``index_date`` ascending.  Each row must carry
    ``computation_metadata['seal']`` - the canonical body that was hashed - so
    the digest can be recomputed independently rather than trusted.
    """
    if not values:
        return ChainVerification(scope="", verified=0, intact=True)

    scope = getattr(values[0], "scope", "") or "NATIONAL"
    previous: str | None = None

    for position, row in enumerate(values):
        seal_body = (row.computation_metadata or {}).get("seal")
        if not seal_body:
            return ChainVerification(scope, position, False, row.provenance_hash, "seal body absent")

        recomputed = ProvenanceSealer.recompute(_revive(seal_body))
        if recomputed != row.provenance_hash:
            return ChainVerification(
                scope, position, False, row.provenance_hash, "provenance hash mismatch"
            )
        if position and row.previous_hash != previous:
            return ChainVerification(
                scope, position, False, row.provenance_hash, "broken back-link"
            )
        previous = row.provenance_hash

    return ChainVerification(scope, len(values), True)


def _revive(body: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the exact types the seal was computed over.

    JSONB round-trips ``Decimal`` and ``date`` as strings; rehydrating them is
    what makes an independent recomputation byte-identical to the original.
    """
    revived: dict[str, Any] = dict(body)
    for key in ("index_value",):
        if isinstance(revived.get(key), str):
            revived[key] = Decimal(revived[key])
    for key in ("index_date",):
        if isinstance(revived.get(key), str):
            revived[key] = date.fromisoformat(revived[key])
    for key in ("computed_at",):
        if isinstance(revived.get(key), str):
            revived[key] = datetime.fromisoformat(revived[key])
    return revived
