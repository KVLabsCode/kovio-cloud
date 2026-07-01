"""Shared fixtures for the kovio-cloud test suite.

These tests exercise the real Postgres-specific query paths (JSONB ``->>`` casts
and the ``?`` / ``has_key`` operator in ``display_insights``), so they need an
actual Postgres. Point ``KOVIO_TEST_DATABASE_URL`` (or ``KOVIO_DATABASE_URL``) at
a throwaway database — e.g. the docker-compose one::

    KOVIO_TEST_DATABASE_URL=postgresql+asyncpg://kovio:kovio@localhost:5432/kovio \\
        pytest

When no database is reachable the DB-backed tests skip rather than fail, so the
suite is safe to run on a machine without Postgres.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

# A dedicated test URL overrides the app default without mutating a prod env var.
_test_url = os.environ.get("KOVIO_TEST_DATABASE_URL")
if _test_url:
    os.environ["KOVIO_DATABASE_URL"] = _test_url


@pytest_asyncio.fixture
async def clean_db():
    """Drop + recreate every table on the configured Postgres before the test,
    then dispose the engine after. Skips the test when no Postgres is reachable.

    Tests use the application's own ``session_scope`` / CLI helpers against the
    same engine, so what they seed is exactly what production code would write.
    """
    from kovio_cloud.db import dispose_engine, get_engine
    from kovio_cloud.models import Base

    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 — any connect failure => skip, not fail
        await dispose_engine()
        pytest.skip(f"no Postgres reachable for kovio-cloud tests ({exc!r})")

    try:
        yield
    finally:
        await dispose_engine()
