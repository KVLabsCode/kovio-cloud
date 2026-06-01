"""SHA256 API-key auth.

API keys carry 190+ bits of entropy (``secrets.token_urlsafe(24)``), so a fast
keyed hash (SHA256 + server-side pepper) is the right primitive — NOT bcrypt,
which is for low-entropy passwords and has buggy 72-byte/version handling in
recent libraries.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import ApiKey, Organization

_PEPPER = os.environ.get("KOVIO_KEY_PEPPER", "kovio-dev-pepper-change-in-prod")


def _hash(key: str) -> str:
    return hashlib.sha256(f"{key}::{_PEPPER}".encode()).hexdigest()


def generate_api_key(env: str = "live") -> tuple[str, str, str]:
    """Return (full_key, key_prefix, key_hash). Show full_key once, store the rest."""

    rand = secrets.token_urlsafe(24)
    full = f"kov_{env}_{rand}"
    return full, full[:16], _hash(full)


@dataclass
class AuthContext:
    api_key_id: uuid.UUID
    org_id: uuid.UUID
    org_kind: str
    fleet_id: uuid.UUID | None
    scopes: list[str] = field(default_factory=list)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _resolve_key(authorization: str | None, session: AsyncSession) -> AuthContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Missing or malformed Authorization header (expected 'Bearer <key>').")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise _unauthorized("Empty bearer token.")

    prefix = token[:16]
    key_hash = _hash(token)

    row = (
        await session.execute(
            select(ApiKey, Organization)
            .join(Organization, Organization.id == ApiKey.org_id)
            .where(
                ApiKey.key_prefix == prefix,
                ApiKey.key_hash == key_hash,
                ApiKey.revoked_at.is_(None),
            )
        )
    ).first()

    if row is None:
        raise _unauthorized("Invalid or revoked API key.")

    api_key, org = row

    # Best-effort last_used_at bump (separate statement; don't fail auth on it).
    await session.execute(
        update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=__now())
    )

    return AuthContext(
        api_key_id=api_key.id,
        org_id=org.id,
        org_kind=org.kind,
        fleet_id=api_key.fleet_id,
        scopes=list(api_key.scopes or []),
    )


def __now():
    # Imported lazily so this module stays import-light.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _require_scope(scope: str):
    async def dependency(
        authorization: str | None = Header(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> AuthContext:
        ctx = await _resolve_key(authorization, session)
        if scope not in ctx.scopes:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"API key is missing the required '{scope}' scope.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return ctx

    return dependency


require_sdk_auth = _require_scope("sdk")
require_admin_auth = _require_scope("admin")
