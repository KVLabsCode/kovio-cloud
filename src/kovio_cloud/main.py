"""FastAPI application. Lifespan starts the background spend processor."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from . import __version__
from .config import get_settings
from .db import dispose_engine, get_logger, get_sessionmaker, init_db
from .routes import admin, advertiser, display, oem, sdk
from .schemas import HealthResponse
from .spend_processor import spend_processor_loop

log = get_logger("kovio_cloud.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    if settings.dev_auto_create_tables:
        # DEV ONLY — Supabase already owns the prod schema.
        await init_db()

    stop_event = asyncio.Event()
    task: asyncio.Task | None = None
    if settings.spend_processor_enabled:
        task = asyncio.create_task(spend_processor_loop(stop_event))
    else:
        log.info("spend_processor_disabled")

    log.info(
        "service_started",
        service=settings.service_name,
        version=__version__,
        environment=settings.environment,
    )

    try:
        yield
    finally:
        stop_event.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await dispose_engine()
        log.info("service_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Kovio Cloud",
        version=__version__,
        description="Control plane for the open robot ad platform.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sdk.router)
    app.include_router(admin.router)
    app.include_router(advertiser.router)
    app.include_router(oem.router)
    app.include_router(display.router)

    @app.get("/healthz", response_model=HealthResponse, tags=["meta"])
    async def healthz() -> HealthResponse:
        db_ok = False
        try:
            sm = get_sessionmaker()
            async with sm() as session:
                await session.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            log.warning("healthz_db_check_failed", exc_info=True)

        return HealthResponse(
            status="ok" if db_ok else "degraded",
            service=settings.service_name,
            version=__version__,
            time=datetime.now(timezone.utc),
            db_ok=db_ok,
        )

    return app


app = create_app()
