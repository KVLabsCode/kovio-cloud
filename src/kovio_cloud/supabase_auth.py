"""Supabase JWT verification for advertiser/OEM web app sessions.

Supabase signs JWTs with HS256 using a project-specific JWT secret. We
verify locally without calling Supabase. The secret is at
Project Settings → API → JWT Secret in the Supabase dashboard.

Different from API key auth (auth.py) — used only by /advertiser/v1/* and
/oem/v1/* routes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer(auto_error=False)
_JWT_SECRET = os.environ.get("KOVIO_SUPABASE_JWT_SECRET", "")
_JWT_AUDIENCE = "authenticated"


@dataclass
class SupabaseUser:
    """A user authenticated via Supabase Auth. Distinct from AuthContext (API key)."""
    supabase_user_id: str
    email: str


def verify_supabase_jwt(token: str) -> Optional[SupabaseUser]:
    if not _JWT_SECRET:
        return None
    try:
        claims = jwt.decode(token, _JWT_SECRET, algorithms=["HS256"], audience=_JWT_AUDIENCE)
    except jwt.PyJWTError:
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
