"""Supabase JWT verification for advertiser/OEM web app sessions.

Supabase can sign session JWTs two ways:
  * **ES256/RS256 (asymmetric)** — the modern default. Verified against the
    project's published JWKS (public keys), fetched from
    {SUPABASE_URL}/auth/v1/.well-known/jwks.json.
  * **HS256 (legacy shared secret)** — verified with KOVIO_SUPABASE_JWT_SECRET.

We inspect the token header's `alg` and verify accordingly, so both work. The
JWKS path is what real magic-link logins use on this project.

Different from API key auth (auth.py) — used only by /advertiser/v1/* and
/oem/v1/* routes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer(auto_error=False)
_JWT_SECRET = os.environ.get("KOVIO_SUPABASE_JWT_SECRET", "")
_SUPABASE_URL = os.environ.get("KOVIO_SUPABASE_URL", "").rstrip("/")
_JWKS_URL = os.environ.get("KOVIO_SUPABASE_JWKS_URL", "") or (
    f"{_SUPABASE_URL}/auth/v1/.well-known/jwks.json" if _SUPABASE_URL else ""
)
_JWT_AUDIENCE = "authenticated"

# PyJWKClient caches keys after the first fetch.
_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> Optional[PyJWKClient]:
    global _jwks_client
    if not _JWKS_URL:
        return None
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_JWKS_URL)
    return _jwks_client


@dataclass
class SupabaseUser:
    """A user authenticated via Supabase Auth. Distinct from AuthContext (API key)."""
    supabase_user_id: str
    email: str


def verify_supabase_jwt(token: str) -> Optional[SupabaseUser]:
    try:
        alg = jwt.get_unverified_header(token).get("alg", "")
    except jwt.PyJWTError:
        return None

    try:
        if alg == "HS256":
            if not _JWT_SECRET:
                return None
            claims = jwt.decode(
                token, _JWT_SECRET, algorithms=["HS256"], audience=_JWT_AUDIENCE
            )
        elif alg in ("ES256", "RS256"):
            client = _get_jwks_client()
            if client is None:
                return None
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=[alg], audience=_JWT_AUDIENCE
            )
        else:
            return None
    except Exception:
        # Any verification/network/key-resolution failure → treat as invalid.
        return None

    sub = claims.get("sub")
    email = claims.get("email", "")
    if not sub:
        return None
    return SupabaseUser(supabase_user_id=sub, email=email)


async def require_supabase_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> SupabaseUser:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    user = verify_supabase_jwt(credentials.credentials)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid supabase token")
    return user
