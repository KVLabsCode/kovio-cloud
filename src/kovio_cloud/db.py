"""Async database engine/session plumbing and structlog configuration."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings

# --- Module-level singletons (lazily initialised) ----------------------------
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_logging_configured = False


def configure_logging() -> None:
    """Configure structlog: pretty console in dev, JSON in production."""

    global _logging_configured
    if _logging_configured:
        return

    settings = get_settings()

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
        log_level = logging.INFO
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
        log_level = logging.DEBUG

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)
    _logging_configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        # Supabase's transaction pooler (pgBouncer, port 6543) rotates the
        # physical backend between transactions, which breaks asyncpg's
        # prepared-statement cache (DuplicatePreparedStatementError). Disable
        # both caches and give every prepared statement a unique name so names
        # never collide on a reused backend. Harmless on a direct connection.
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
            connect_args={
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
            },
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def init_db() -> None:
    """DEV ONLY: create all tables. Gated by KOVIO_DEV_AUTO_CREATE_TABLES.

    Production never calls this with auto-create — Supabase already owns the
    schema. The local docker-compose Postgres has no tables, so dev needs it.
    """

    from .models import Base  # local import avoids a circular import at load time

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    get_logger("kovio_cloud.db").info("database_initialised")


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone transactional session (used by the CLI/bootstrap)."""

    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""

    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
