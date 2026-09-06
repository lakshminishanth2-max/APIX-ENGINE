"""Declarative base and shared column types for the APIx relational schema.

An explicit naming convention is mandatory here: Alembic autogenerate produces
unusable migrations against PostgreSQL unless constraint names are deterministic,
and a statistical publication cannot tolerate a migration that silently drops and
recreates a constraint under a different name.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Final

from sqlalchemy import BigInteger, DateTime, MetaData, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

__all__ = ["Base", "JSONBType", "money", "sha256", "timestamptz", "utcnow"]

_UTC: Final = timezone.utc

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSONB on PostgreSQL, plain JSON elsewhere (SQLite in unit tests).
JSONBType = JSON().with_variant(JSONB(astext_type=String()), "postgresql")


def utcnow() -> datetime:
    return datetime.now(_UTC)


# --------------------------------------------------------------------------- #
# Reusable annotated column types
# --------------------------------------------------------------------------- #
#: Indian fares never need more than 14 integer digits; scale 2 is the paise.
#: NUMERIC keeps the value exact end-to-end - DOUBLE PRECISION is prohibited.
money = Annotated[Decimal, mapped_column(Numeric(16, 2))]

#: Statistical quantities (index levels, weights, scores) carry more scale.
statistic = Annotated[Decimal, mapped_column(Numeric(20, 8))]

sha256 = Annotated[str, mapped_column(String(64))]

timestamptz = Annotated[datetime, mapped_column(DateTime(timezone=True))]

bigint_pk = Annotated[int, mapped_column(BigInteger, primary_key=True, autoincrement=True)]


class Base(DeclarativeBase):
    """Root of every ORM model in the data core."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        Decimal: Numeric(20, 8),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONBType,
        list[Any]: JSONBType,
    }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        pk = getattr(self, "id", None) or getattr(self, "digest", None)
        return f"<{type(self).__name__} {pk}>"


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
