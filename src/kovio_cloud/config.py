"""Application configuration via pydantic-settings (env prefix ``KOVIO_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Every field maps to a ``KOVIO_*`` env var."""

    model_config = SettingsConfigDict(
        env_prefix="KOVIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core service identity -------------------------------------------------
    service_name: str = "kovio-cloud"
    environment: str = "dev"  # "dev" -> pretty logs, anything else -> JSON logs

    # --- Database --------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://kovio:kovio@localhost:5432/kovio"
    # Local-dev only: run Base.metadata.create_all() on startup because the dev
    # docker-compose Postgres has no tables. NEVER true in prod — Supabase owns
    # the schema there.
    dev_auto_create_tables: bool = False

    # --- Auth ------------------------------------------------------------------
    # The auth module reads KOVIO_KEY_PEPPER directly (see auth.py) so the hashing
    # helper stays import-light. This field documents and validates the var too.
    key_pepper: str = "kovio-dev-pepper-change-in-prod"

    # Supabase Auth JWT secret (HS256 legacy), used to verify human web-app
    # sessions signed with the shared secret. Dashboard: Project Settings → API.
    supabase_jwt_secret: str = ""
    # Supabase project URL — used to derive the JWKS endpoint for verifying
    # modern ES256/RS256-signed session tokens. Read directly in supabase_auth.py.
    supabase_url: str = ""

    # --- Stripe (advertiser payments) -----------------------------------------
    # Secret key: sk_test_… in test mode, sk_live_… in prod. Empty disables the
    # payment endpoints (they return 503) so dev works without Stripe configured.
    stripe_secret_key: str = ""
    # Signing secret for /advertiser/v1/stripe/webhook (whsec_…), from the
    # Stripe dashboard webhook endpoint.
    stripe_webhook_secret: str = ""
    # Advertiser web-app base URL, used for Checkout success/cancel return links.
    web_app_url: str = "https://app.kovio.dev"

    # --- Spend processor -------------------------------------------------------
    spend_processor_enabled: bool = True
    spend_processor_interval_seconds: int = 60

    # --- HTTP server (used by `kovio-cloud serve`) -----------------------------
    host: str = "0.0.0.0"
    port: int = 8080

    # --- CORS ------------------------------------------------------------------
    cors_origins: list[str] = [
        "https://kovio.dev",
        "https://app.kovio.dev",
        "http://localhost:3000",
    ]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() not in ("dev", "development", "local", "test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
