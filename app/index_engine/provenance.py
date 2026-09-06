"""SHA-256 lineage tracking for every published index level.

A price index is a regulatory artefact: months later somebody will ask why the
DEL-BOM economy index printed 118.4 on a given day.  Answering that requires
more than a number in a table - it requires the exact inputs, the exact
methodology, and proof that neither has been altered since.

This module provides that proof:

* **Canonical serialisation** - one deterministic byte representation of any
  payload (sorted keys, exact ``Decimal`` text, ISO-8601 timestamps, no float
  ever), so the same logical value always produces the same digest.
* **A Merkle DAG** - every source payload, cleaned record, stratum index and
  headline level is a :class:`ProvenanceNode` whose digest binds its operation,
  its parameters and its parents' digests.  Changing any input anywhere changes
  the root.
* **A hash chain** - each :class:`ProvenanceRecord` embeds the digest of its
  predecessor for the same index series, so a silently rewritten historical
  print breaks verification of every record after it.

Persistence uses SQLAlchemy 2.0 async and is append-only by construction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Final, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

try:  # The project's declarative base, when running inside the app.
    from app.db.base import Base  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - keeps the module importable standalone
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):  # type: ignore[no-redef]
        pass


__all__ = [
    "ChainVerification",
    "LineageBuilder",
    "LineageError",
    "LineageGraph",
    "NodeKind",
    "ProvenanceRecord",
    "ProvenanceRecordORM",
    "ProvenanceRepository",
    "ProvenanceNode",
    "canonical_bytes",
    "canonical_json",
    "merkle_root",
    "sha256_hex",
]

_UTC: Final = timezone.utc
_EMPTY_DIGEST: Final[str] = hashlib.sha256(b"").hexdigest()
_HEX64: Final[str] = r"^[0-9a-f]{64}$"


class LineageError(RuntimeError):
    """Raised when a lineage graph or hash chain fails verification."""


# --------------------------------------------------------------------------- #
# Canonical serialisation
# --------------------------------------------------------------------------- #
def _canonical(value: Any) -> str:
    """Deterministic textual form of ``value``.

    ``float`` is rejected outright: binary floating point is not reproducible
    across platforms and has no place in a monetary lineage.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        raise TypeError("float is not permitted in canonical lineage payloads; use Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal in lineage payload: {value}")
        return f'"{format(value.normalize(), "f")}"'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=_UTC)
        return f'"{moment.astimezone(_UTC).isoformat()}"'
    if isinstance(value, date):
        return f'"{value.isoformat()}"'
    if isinstance(value, UUID):
        return f'"{value}"'
    if isinstance(value, Mapping):
        items = ",".join(f"{_canonical(str(k))}:{_canonical(v)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
        return "{" + items + "}"
    if isinstance(value, (set, frozenset)):
        return "[" + ",".join(sorted(_canonical(v) for v in value)) + "]"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump())
    raise TypeError(f"unsupported type in canonical payload: {type(value).__name__}")


def canonical_json(payload: Any) -> str:
    """Stable, sorted, float-free JSON text used as digest input."""
    return _canonical(payload)


def canonical_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def sha256_hex(payload: Any) -> str:
    """SHA-256 of a payload's canonical form."""
    if isinstance(payload, (bytes, bytearray)):
        return hashlib.sha256(bytes(payload)).hexdigest()
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def merkle_root(digests: Sequence[str]) -> str:
    """Binary Merkle root over an *ordered* list of hex digests."""
    if not digests:
        return _EMPTY_DIGEST
    level = [bytes.fromhex(d) for d in digests]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()


# --------------------------------------------------------------------------- #
# Lineage graph
# --------------------------------------------------------------------------- #
class NodeKind(StrEnum):
    SOURCE_PAYLOAD = "source_payload"
    RAW_QUOTE = "raw_quote"
    NORMALIZED = "normalized"
    DEDUPLICATED = "deduplicated"
    OUTLIER_SCREENED = "outlier_screened"
    WEIGHT_SET = "weight_set"
    ELEMENTARY_INDEX = "elementary_index"
    AGGREGATE_INDEX = "aggregate_index"
    METHODOLOGY = "methodology"


class ProvenanceNode(BaseModel):
    """One immutable step in the derivation of an index level."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: UUID = Field(default_factory=uuid4)
    kind: NodeKind
    operation: str
    digest: str = Field(pattern=_HEX64)
    payload_digest: str = Field(pattern=_HEX64)
    params_digest: str = Field(pattern=_HEX64)
    parents: tuple[str, ...] = ()
    label: str | None = None
    metadata: Mapping[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(_UTC))

    @staticmethod
    def compute_digest(
        *,
        kind: NodeKind,
        operation: str,
        payload_digest: str,
        params_digest: str,
        parents: Sequence[str],
    ) -> str:
        """Bind kind, operation, parameters, payload and parents into one hash."""
        return sha256_hex(
            {
                "kind": kind,
                "operation": operation,
                "payload": payload_digest,
                "params": params_digest,
                # Parents are order-independent: sort so batch order cannot
                # change the digest of an otherwise identical derivation.
                "parents": sorted(parents),
            }
        )


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """Immutable, verifiable DAG of :class:`ProvenanceNode`."""

    nodes: Mapping[str, ProvenanceNode]
    order: tuple[str, ...]

    @property
    def root_digest(self) -> str:
        """Merkle root over the graph in insertion order."""
        return merkle_root(self.order)

    @property
    def leaves(self) -> tuple[ProvenanceNode, ...]:
        referenced = {p for node in self.nodes.values() for p in node.parents}
        return tuple(n for d, n in self.nodes.items() if d not in referenced)

    def ancestors(self, digest: str) -> tuple[str, ...]:
        """Transitive parents of a node, deepest last, deduplicated."""
        seen: list[str] = []
        stack = list(self.nodes[digest].parents)
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.append(current)
            stack.extend(self.nodes[current].parents)
        return tuple(seen)

    def verify(self) -> None:
        """Recompute every digest and every edge; raise on any mismatch."""
        for digest, node in self.nodes.items():
            expected = ProvenanceNode.compute_digest(
                kind=node.kind,
                operation=node.operation,
                payload_digest=node.payload_digest,
                params_digest=node.params_digest,
                parents=node.parents,
            )
            if expected != digest or node.digest != digest:
                raise LineageError(f"node digest mismatch: stored={digest} recomputed={expected}")
            for parent in node.parents:
                if parent not in self.nodes:
                    raise LineageError(f"dangling lineage edge {digest} -> {parent}")

    def to_edges(self) -> tuple[tuple[str, str], ...]:
        return tuple((parent, digest) for digest, node in self.nodes.items() for parent in node.parents)

    def describe(self) -> list[dict[str, Any]]:
        return [self.nodes[d].model_dump(mode="json") for d in self.order]


class LineageBuilder:
    """Accumulates provenance nodes while a calculation runs.

    Nodes are content-addressed, so recording the same derivation twice is a
    no-op rather than a duplicate.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, ProvenanceNode] = {}
        self._order: list[str] = []

    def __len__(self) -> int:
        return len(self._nodes)

    def add_source(
        self,
        *,
        payload: Any,
        label: str,
        kind: NodeKind = NodeKind.SOURCE_PAYLOAD,
        metadata: Mapping[str, str] | None = None,
    ) -> ProvenanceNode:
        """Record a root node: raw upstream data with no parents."""
        return self._add(
            kind=kind,
            operation="ingest",
            payload_digest=payload if _is_digest(payload) else sha256_hex(payload),
            params={},
            parents=(),
            label=label,
            metadata=metadata,
        )

    def add_transform(
        self,
        *,
        operation: str,
        parents: Sequence[ProvenanceNode | str],
        payload: Any,
        params: Mapping[str, Any] | None = None,
        kind: NodeKind = NodeKind.NORMALIZED,
        label: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ProvenanceNode:
        """Record a derived node bound to its inputs and parameters."""
        return self._add(
            kind=kind,
            operation=operation,
            payload_digest=payload if _is_digest(payload) else sha256_hex(payload),
            params=params or {},
            parents=tuple(p.digest if isinstance(p, ProvenanceNode) else p for p in parents),
            label=label,
            metadata=metadata,
        )

    def build(self) -> LineageGraph:
        graph = LineageGraph(nodes=dict(self._nodes), order=tuple(self._order))
        graph.verify()
        return graph

    # -- internals ---------------------------------------------------------- #
    def _add(
        self,
        *,
        kind: NodeKind,
        operation: str,
        payload_digest: str,
        params: Mapping[str, Any],
        parents: Sequence[str],
        label: str | None,
        metadata: Mapping[str, str] | None,
    ) -> ProvenanceNode:
        params_digest = sha256_hex(params)
        digest = ProvenanceNode.compute_digest(
            kind=kind,
            operation=operation,
            payload_digest=payload_digest,
            params_digest=params_digest,
            parents=parents,
        )
        existing = self._nodes.get(digest)
        if existing is not None:
            return existing

        node = ProvenanceNode(
            kind=kind,
            operation=operation,
            digest=digest,
            payload_digest=payload_digest,
            params_digest=params_digest,
            parents=tuple(parents),
            label=label,
            metadata=dict(metadata or {}),
        )
        self._nodes[digest] = node
        self._order.append(digest)
        return node


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# --------------------------------------------------------------------------- #
# Sealed record + hash chain
# --------------------------------------------------------------------------- #
class ProvenanceRecord(BaseModel):
    """Tamper-evident receipt for one published index level."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID = Field(default_factory=uuid4)
    index_id: str
    period: str
    index_level: Decimal
    methodology_version: str
    methodology_digest: str = Field(pattern=_HEX64)
    weight_set_digest: str | None = Field(default=None, pattern=_HEX64)
    input_root: str = Field(pattern=_HEX64)
    lineage_root: str = Field(pattern=_HEX64)
    node_count: int = Field(ge=0)
    previous_record_digest: str | None = Field(default=None, pattern=_HEX64)
    record_digest: str = Field(pattern=_HEX64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(_UTC))
    metadata: Mapping[str, str] = Field(default_factory=dict)

    @classmethod
    def seal(
        cls,
        *,
        index_id: str,
        period: str,
        index_level: Decimal,
        methodology_version: str,
        methodology_digest: str,
        graph: LineageGraph,
        input_digests: Sequence[str],
        weight_set_digest: str | None = None,
        previous_record_digest: str | None = None,
        metadata: Mapping[str, str] | None = None,
        created_at: datetime | None = None,
    ) -> Self:
        """Compute the record digest over every field that must not change."""
        moment = created_at or datetime.now(_UTC)
        body = {
            "index_id": index_id,
            "period": period,
            "index_level": index_level,
            "methodology_version": methodology_version,
            "methodology_digest": methodology_digest,
            "weight_set_digest": weight_set_digest,
            "input_root": merkle_root(list(input_digests)),
            "lineage_root": graph.root_digest,
            "node_count": len(graph.nodes),
            "previous_record_digest": previous_record_digest,
            "created_at": moment,
            "metadata": dict(metadata or {}),
        }
        return cls(
            index_id=index_id,
            period=period,
            index_level=index_level,
            methodology_version=methodology_version,
            methodology_digest=methodology_digest,
            weight_set_digest=weight_set_digest,
            input_root=body["input_root"],
            lineage_root=graph.root_digest,
            node_count=len(graph.nodes),
            previous_record_digest=previous_record_digest,
            record_digest=sha256_hex(body),
            created_at=moment,
            metadata=dict(metadata or {}),
        )

    def recompute_digest(self) -> str:
        return sha256_hex(
            {
                "index_id": self.index_id,
                "period": self.period,
                "index_level": self.index_level,
                "methodology_version": self.methodology_version,
                "methodology_digest": self.methodology_digest,
                "weight_set_digest": self.weight_set_digest,
                "input_root": self.input_root,
                "lineage_root": self.lineage_root,
                "node_count": self.node_count,
                "previous_record_digest": self.previous_record_digest,
                "created_at": self.created_at,
                "metadata": dict(self.metadata),
            }
        )

    def verify_self(self) -> bool:
        return self.recompute_digest() == self.record_digest


@dataclass(frozen=True, slots=True)
class ChainVerification:
    index_id: str
    verified: int
    intact: bool
    broken_at: str | None = None
    reason: str | None = None


def verify_chain(records: Sequence[ProvenanceRecord]) -> ChainVerification:
    """Verify digests and back-links across an ordered run of records."""
    if not records:
        return ChainVerification(index_id="", verified=0, intact=True)

    index_id = records[0].index_id
    previous: str | None = records[0].previous_record_digest
    for position, record in enumerate(records):
        if record.index_id != index_id:
            return ChainVerification(index_id, position, False, record.record_digest, "index_id changed mid-chain")
        if not record.verify_self():
            return ChainVerification(index_id, position, False, record.record_digest, "record digest mismatch")
        if position and record.previous_record_digest != previous:
            return ChainVerification(index_id, position, False, record.record_digest, "broken back-link")
        previous = record.record_digest
    return ChainVerification(index_id, len(records), True)


# --------------------------------------------------------------------------- #
# Persistence (SQLAlchemy 2.0 async, append-only)
# --------------------------------------------------------------------------- #
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ProvenanceRecordORM(Base):
    """Append-only lineage ledger.  Rows are never updated or deleted."""

    __tablename__ = "index_provenance"
    __table_args__ = (Index("ix_index_provenance_series", "index_id", "sequence"),)

    record_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    record_id: Mapped[str] = mapped_column(String(36), unique=True)
    index_id: Mapped[str] = mapped_column(String(128), index=True)
    period: Mapped[str] = mapped_column(String(32))
    index_level: Mapped[str] = mapped_column(String(40))
    methodology_version: Mapped[str] = mapped_column(String(64), index=True)
    methodology_digest: Mapped[str] = mapped_column(String(64))
    weight_set_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    input_root: Mapped[str] = mapped_column(String(64))
    lineage_root: Mapped[str] = mapped_column(String(64))
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    previous_record_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    lineage: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_record(self) -> ProvenanceRecord:
        return ProvenanceRecord(
            record_id=UUID(self.record_id),
            index_id=self.index_id,
            period=self.period,
            index_level=Decimal(self.index_level),
            methodology_version=self.methodology_version,
            methodology_digest=self.methodology_digest,
            weight_set_digest=self.weight_set_digest,
            input_root=self.input_root,
            lineage_root=self.lineage_root,
            node_count=self.node_count,
            previous_record_digest=self.previous_record_digest,
            record_digest=self.record_digest,
            created_at=self.created_at.replace(tzinfo=self.created_at.tzinfo or _UTC),
            metadata=dict((self.lineage or {}).get("metadata", {})),
        )


class ProvenanceRepository:
    """Append-only access to the lineage ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def head(self, index_id: str) -> ProvenanceRecordORM | None:
        stmt = (
            select(ProvenanceRecordORM)
            .where(ProvenanceRecordORM.index_id == index_id)
            .order_by(ProvenanceRecordORM.sequence.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def head_digest(self, index_id: str) -> str | None:
        head = await self.head(index_id)
        return head.record_digest if head else None

    async def append(
        self,
        record: ProvenanceRecord,
        *,
        graph: LineageGraph | None = None,
    ) -> ProvenanceRecordORM:
        """Persist a sealed record, enforcing the hash chain at write time."""
        if not record.verify_self():
            raise LineageError(f"refusing to persist record {record.record_id}: digest mismatch")

        expected_previous = await self.head_digest(record.index_id)
        if record.previous_record_digest != expected_previous:
            raise LineageError(
                f"chain break for {record.index_id!r}: record points at "
                f"{record.previous_record_digest!r} but head is {expected_previous!r}"
            )

        row = ProvenanceRecordORM(
            record_digest=record.record_digest,
            record_id=str(record.record_id),
            index_id=record.index_id,
            period=record.period,
            index_level=format(record.index_level, "f"),
            methodology_version=record.methodology_version,
            methodology_digest=record.methodology_digest,
            weight_set_digest=record.weight_set_digest,
            input_root=record.input_root,
            lineage_root=record.lineage_root,
            node_count=record.node_count,
            previous_record_digest=record.previous_record_digest,
            lineage={
                "metadata": dict(record.metadata),
                "nodes": graph.describe() if graph is not None else [],
            },
            created_at=record.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def series(self, index_id: str, *, limit: int = 1_000) -> tuple[ProvenanceRecord, ...]:
        stmt = (
            select(ProvenanceRecordORM)
            .where(ProvenanceRecordORM.index_id == index_id)
            .order_by(ProvenanceRecordORM.sequence.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(row.to_record() for row in rows)

    async def get(self, record_digest: str) -> ProvenanceRecord | None:
        row = await self._session.get(ProvenanceRecordORM, record_digest)
        return row.to_record() if row else None

    async def verify(self, index_id: str, *, limit: int = 1_000) -> ChainVerification:
        return verify_chain(await self.series(index_id, limit=limit))
