"""Async engine, session factory and unit-of-work helpers.

One engine per process.  FastAPI request handlers take a session through
:func:`get_session`; Celery tasks - which have no request scope - use the
:func:`session_scope` context manager, which commits on success and rolls back
on any exception so a half-written collection run can never be published.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, Final

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings

__all__ = [
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "session_scope",
]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_UTC_TIMEZONE_SQL: Final[str] = "UTC"


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Lazily build the process-wide async engine."""
    global _engine
    if _engine is None:
        cfg = settings or get_settings()
        # NullPool under Celery: forked workers must not inherit live sockets.
        pool_kwargs: dict[str, Any] = (
            {"poolclass": NullPool}
            if cfg.environment == "ci"
            else {
                "pool_size": cfg.database_pool_size,
                "max_overflow": cfg.database_max_overflow,
                "pool_pre_ping": True,
                "pool_recycle": 1_800,
            }
        )
        _engine = create_async_engine(
            str(cfg.database_url),
            echo=cfg.database_echo,
            future=True,
            connect_args={"server_settings": {"timezone": _UTC_TIMEZONE_SQL}},
            **pool_kwargs,
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for workers and scripts."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close pooled connections on shutdown (FastAPI lifespan / worker exit)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
