"""``/oem/v1/*`` — fleet-operator endpoints for the OEM web app.

Mirror of the advertiser router: Supabase-JWT auth, scoped to the logged-in
user's OEM org (an ``organizations`` row with ``kind='oem'``). An OEM owns
fleets; each fleet owns robots and api_keys. Revenue accrues into
``organizations.pending_payout_cents`` via the spend processor.

API-key SECRETS are returned only by the mint endpoint, exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audience import audience_summary
from ..auth import generate_api_key
from ..db import get_session
from ..models import ApiKey, Campaign, Fleet, Impression, Organization, Robot, User
from ..supabase_auth import SupabaseUser, require_supabase_user

router = APIRouter(prefix="/oem/v1", tags=["oem"])

_HEARTBEAT_ACTIVE = timedelta(minutes=5)


# --- helpers -----------------------------------------------------------------
def _coded(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


def _user_dict(u: User) -> dict[str, Any]:
    return {"id": u.id, "email": u.email, "role": u.role}


def _oem_org_dict(org: Organization) -> dict[str, Any]:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "kind": org.kind,
        "pending_payout_cents": org.pending_payout_cents,
        "lifetime_payout_cents": org.lifetime_payout_cents,
        "stripe_connect_id": org.stripe_connect_id,
        "created_at": org.created_at,
    }


def _fleet_dict(f: Fleet, **extra: Any) -> dict[str, Any]:
    d = {
        "id": f.id,
        "name": f.name,
        "region": f.region,
        "blocked_categories": list(f.blocked_categories or []),
        "blocked_advertisers": list(f.blocked_advertisers or []),
        "revenue_share_pct": float(f.revenue_share_pct),
        "created_at": f.created_at,
    }
    d.update(extra)
    return d


async def _lookup_user(supa: SupabaseUser, session: AsyncSession) -> User | None:
    return (
        await session.execute(
            select(User).where(User.supabase_user_id == uuid.UUID(supa.supabase_user_id))
        )
    ).scalar_one_or_none()


async def _oem_context(
    supa: SupabaseUser, session: AsyncSession
) -> tuple[User | None, Organization | None, JSONResponse | None]:
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return None, None, _coded(404, "not_onboarded", "complete onboarding first")
    org = (
        await session.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one()
    if org.kind != "oem":
        return None, None, _coded(
            403, "wrong_user_kind", "this user is an advertiser, not an OEM"
        )
    return user, org, None


async def _scoped_fleet(
    fleet_pk: uuid.UUID, org: Organization, session: AsyncSession
) -> tuple[Fleet | None, JSONResponse | None]:
    fleet = (
        await session.execute(select(Fleet).where(Fleet.id == fleet_pk))
    ).scalar_one_or_none()
    if fleet is None or fleet.org_id != org.id:
        return None, _coded(404, "not_found", "fleet not found")
    return fleet, None


def _zero_filled_by_day(rows: list[Any], now: datetime) -> list[dict[str, Any]]:
    """rows: (date, impressions, revenue_cents). Fill the last 30 days with zeros."""

    by = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    out = []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).date()
        imp, rev = by.get(day, (0, 0))
        out.append({"date": str(day), "impressions": imp, "revenue_cents": rev})
    return out


# --- request bodies ----------------------------------------------------------
class OnboardingBody(BaseModel):
    org_name: str
    org_slug: str


class FleetCreateBody(BaseModel):
    name: str
    region: str | None = None


class FleetPatchBody(BaseModel):
    name: str | None = None
    region: str | None = None
    blocked_categories: list[str] | None = None
    blocked_advertisers: list[uuid.UUID] | None = None


class ApiKeyCreateBody(BaseModel):
    name: str


# --- GET /me -----------------------------------------------------------------
@router.get("/me")
async def me(
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    return {"user": _user_dict(user), "org": _oem_org_dict(org)}


# --- POST /onboarding --------------------------------------------------------
@router.post("/onboarding")
async def onboarding(
    body: OnboardingBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    if await _lookup_user(supa, session) is not None:
        return _coded(409, "already_onboarded", "this account is already linked to an org")

    org = Organization(name=body.org_name, slug=body.org_slug, kind="oem")
    session.add(org)
    user = User(
        supabase_user_id=uuid.UUID(supa.supabase_user_id),
        email=supa.email,
        organization=org,
        role="admin",
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return _coded(409, "slug_taken", "that org slug is already in use")

    return {"user": _user_dict(user), "org": _oem_org_dict(org)}


# --- GET /dashboard ----------------------------------------------------------
@router.get("/dashboard")
async def dashboard(
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    org_id = org.id
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_30d = now - timedelta(days=30)

    async def _scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    total_fleets = await _scalar(
        select(func.count()).select_from(Fleet).where(Fleet.org_id == org_id)
    )
    total_robots = await _scalar(
        select(func.count())
        .select_from(Robot)
        .join(Fleet, Fleet.id == Robot.fleet_id)
        .where(Fleet.org_id == org_id)
    )
    active_robots = await _scalar(
        select(func.count())
        .select_from(Robot)
        .join(Fleet, Fleet.id == Robot.fleet_id)
        .where(Fleet.org_id == org_id, Robot.last_heartbeat >= now - _HEARTBEAT_ACTIVE)
    )

    def _imp_count(since) -> Any:
        return select(func.count()).select_from(Impression).where(
            Impression.oem_org_id == org_id, Impression.timestamp >= since
        )

    def _rev_sum(since) -> Any:
        return select(func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0)).where(
            Impression.oem_org_id == org_id, Impression.timestamp >= since
        )

    impressions_24h = await _scalar(_imp_count(since_24h))
    impressions_30d = await _scalar(_imp_count(since_30d))
    revenue_24h = await _scalar(_rev_sum(since_24h))
    revenue_30d = await _scalar(_rev_sum(since_30d))

    day = func.date(Impression.timestamp)
    by_day_rows = (
        await session.execute(
            select(
                day,
                func.count(),
                func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0),
            )
            .where(Impression.oem_org_id == org_id, Impression.timestamp >= since_30d)
            .group_by(day)
        )
    ).all()
    by_day = _zero_filled_by_day(by_day_rows, now)

    by_fleet_rows = (
        await session.execute(
            select(
                Fleet.id,
                Fleet.name,
                func.count(Impression.id),
                func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0),
                func.coalesce(func.avg(Impression.person_count), 0),
                func.coalesce(func.avg(Impression.attended_count), 0),
            )
            .select_from(Fleet)
            .outerjoin(
                Impression,
                (Impression.fleet_id == Fleet.id) & (Impression.timestamp >= since_30d),
            )
            .where(Fleet.org_id == org_id)
            .group_by(Fleet.id, Fleet.name)
            .order_by(func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0).desc())
        )
    ).all()
    by_fleet = [
        {
            "fleet_id": str(r[0]),
            "fleet_name": r[1],
            "impressions_30d": int(r[2]),
            "revenue_30d_cents": int(r[3]),
            "avg_reach_30d": round(float(r[4]), 1),
            "avg_attended_30d": round(float(r[5]), 1),
        }
        for r in by_fleet_rows
    ]

    recent_rows = (
        await session.execute(
            select(
                Impression.id,
                Campaign.name,
                Campaign.advertiser,
                Fleet.name,
                Impression.revenue_to_oem_cents,
                Impression.timestamp,
                Robot.external_id,
            )
            .join(Campaign, Campaign.id == Impression.campaign_id)
            .join(Fleet, Fleet.id == Impression.fleet_id)
            .outerjoin(Robot, Robot.id == Impression.robot_id)
            .where(Impression.oem_org_id == org_id)
            .order_by(Impression.timestamp.desc())
            .limit(10)
        )
    ).all()
    recent_impressions = [
        {
            "id": str(r[0]),
            "campaign_name": r[1],
            "campaign_advertiser": r[2],
            "fleet_name": r[3],
            "revenue_to_oem_cents": int(r[4]),
            "timestamp": r[5],
            "robot_external_id": r[6] or "unregistered",
        }
        for r in recent_rows
    ]

    audience_24h = await audience_summary(
        session, Impression.oem_org_id == org_id, Impression.timestamp >= since_24h
    )
    audience_30d = await audience_summary(
        session, Impression.oem_org_id == org_id, Impression.timestamp >= since_30d
    )

    return {
        "pending_payout_cents": org.pending_payout_cents,
        "lifetime_payout_cents": org.lifetime_payout_cents,
        "impressions_24h": impressions_24h,
        "impressions_30d": impressions_30d,
        "revenue_24h_cents": revenue_24h,
        "revenue_30d_cents": revenue_30d,
        "total_fleets": total_fleets,
        "total_robots": total_robots,
        "active_robots": active_robots,
        "audience_24h": audience_24h,
        "audience_30d": audience_30d,
        "by_day": by_day,
        "by_fleet": by_fleet,
        "recent_impressions": recent_impressions,
    }


# --- GET /fleets -------------------------------------------------------------
@router.get("/fleets")
async def list_fleets(
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)

    fleets = (
        await session.execute(
            select(Fleet).where(Fleet.org_id == org.id).order_by(Fleet.created_at.desc())
        )
    ).scalars().all()

    # robot counts per fleet
    robot_rows = (
        await session.execute(
            select(Robot.fleet_id, func.count())
            .join(Fleet, Fleet.id == Robot.fleet_id)
            .where(Fleet.org_id == org.id)
            .group_by(Robot.fleet_id)
        )
    ).all()
    robot_counts = {r[0]: int(r[1]) for r in robot_rows}

    # 24h impressions + revenue per fleet
    imp_rows = (
        await session.execute(
            select(
                Impression.fleet_id,
                func.count(),
                func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0),
            )
            .where(Impression.oem_org_id == org.id, Impression.timestamp >= since_24h)
            .group_by(Impression.fleet_id)
        )
    ).all()
    imp_map = {r[0]: (int(r[1]), int(r[2])) for r in imp_rows}

    out = [
        _fleet_dict(
            f,
            robot_count=robot_counts.get(f.id, 0),
            impressions_24h=imp_map.get(f.id, (0, 0))[0],
            revenue_24h_cents=imp_map.get(f.id, (0, 0))[1],
        )
        for f in fleets
    ]
    return {"fleets": out}


# --- POST /fleets ------------------------------------------------------------
@router.post("/fleets", status_code=status.HTTP_201_CREATED)
async def create_fleet(
    body: FleetCreateBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    fleet = Fleet(org_id=org.id, name=body.name, region=body.region)
    session.add(fleet)
    await session.flush()
    return JSONResponse(status_code=201, content=_jsonable(_fleet_dict(fleet, robot_count=0)))


# --- GET /fleets/{id} --------------------------------------------------------
@router.get("/fleets/{fleet_pk}")
async def fleet_detail(
    fleet_pk: uuid.UUID,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    fleet, ferr = await _scoped_fleet(fleet_pk, org, session)
    if ferr is not None:
        return ferr

    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_30d = now - timedelta(days=30)

    robots = (
        await session.execute(
            select(Robot).where(Robot.fleet_id == fleet.id).order_by(Robot.created_at.desc())
        )
    ).scalars().all()
    keys = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.fleet_id == fleet.id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()

    async def _scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    def _imp(since):
        return select(func.count()).select_from(Impression).where(
            Impression.fleet_id == fleet.id, Impression.timestamp >= since
        )

    def _rev(since):
        return select(func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0)).where(
            Impression.fleet_id == fleet.id, Impression.timestamp >= since
        )

    day = func.date(Impression.timestamp)
    by_day_rows = (
        await session.execute(
            select(day, func.count(), func.coalesce(func.sum(Impression.revenue_to_oem_cents), 0))
            .where(Impression.fleet_id == fleet.id, Impression.timestamp >= since_30d)
            .group_by(day)
        )
    ).all()

    return {
        "fleet": _fleet_dict(fleet),
        "robots": [
            {
                "id": str(r.id),
                "external_id": r.external_id,
                "status": r.status,
                "last_heartbeat": r.last_heartbeat,
                "created_at": r.created_at,
            }
            for r in robots
        ],
        "api_keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "key_prefix": k.key_prefix,
                "scopes": list(k.scopes or []),
                "last_used_at": k.last_used_at,
                "created_at": k.created_at,
            }
            for k in keys
        ],
        "stats": {
            "impressions_24h": await _scalar(_imp(since_24h)),
            "impressions_30d": await _scalar(_imp(since_30d)),
            "revenue_24h_cents": await _scalar(_rev(since_24h)),
            "revenue_30d_cents": await _scalar(_rev(since_30d)),
            "by_day": _zero_filled_by_day(by_day_rows, now),
            "audience_30d": await audience_summary(
                session, Impression.fleet_id == fleet.id, Impression.timestamp >= since_30d
            ),
        },
    }


# --- PATCH /fleets/{id} ------------------------------------------------------
@router.patch("/fleets/{fleet_pk}")
async def update_fleet(
    fleet_pk: uuid.UUID,
    body: FleetPatchBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    fleet, ferr = await _scoped_fleet(fleet_pk, org, session)
    if ferr is not None:
        return ferr

    if body.name is not None:
        fleet.name = body.name
    if body.region is not None:
        fleet.region = body.region
    if body.blocked_categories is not None:
        fleet.blocked_categories = body.blocked_categories
    if body.blocked_advertisers is not None:
        fleet.blocked_advertisers = body.blocked_advertisers
    await session.flush()
    return JSONResponse(content=_jsonable(_fleet_dict(fleet)))


# --- POST /fleets/{id}/api-keys (mint — secret returned ONCE) ----------------
@router.post("/fleets/{fleet_pk}/api-keys", status_code=status.HTTP_201_CREATED)
async def mint_api_key(
    fleet_pk: uuid.UUID,
    body: ApiKeyCreateBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    fleet, ferr = await _scoped_fleet(fleet_pk, org, session)
    if ferr is not None:
        return ferr

    full, prefix, key_hash = generate_api_key()
    key = ApiKey(
        org_id=org.id,
        fleet_id=fleet.id,
        name=body.name,
        key_prefix=prefix,
        key_hash=key_hash,
        scopes=["sdk"],
    )
    session.add(key)
    await session.flush()
    return JSONResponse(
        status_code=201,
        content=_jsonable(
            {
                "id": str(key.id),
                "name": key.name,
                "key_prefix": key.key_prefix,
                "secret": full,  # shown exactly once
                "scopes": list(key.scopes),
                "created_at": key.created_at,
            }
        ),
    )


# --- GET /fleets/{id}/api-keys (no secrets) ----------------------------------
@router.get("/fleets/{fleet_pk}/api-keys")
async def list_api_keys(
    fleet_pk: uuid.UUID,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    fleet, ferr = await _scoped_fleet(fleet_pk, org, session)
    if ferr is not None:
        return ferr

    keys = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.fleet_id == fleet.id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    return {
        "api_keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "key_prefix": k.key_prefix,
                "scopes": list(k.scopes or []),
                "last_used_at": k.last_used_at,
                "created_at": k.created_at,
            }
            for k in keys
        ]
    }


# --- DELETE /fleets/{id}/api-keys/{key_id} (soft delete) ---------------------
@router.delete("/fleets/{fleet_pk}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    fleet_pk: uuid.UUID,
    key_id: uuid.UUID,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    _, org, err = await _oem_context(supa, session)
    if err is not None:
        return err
    fleet, ferr = await _scoped_fleet(fleet_pk, org, session)
    if ferr is not None:
        return ferr

    key = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.fleet_id == fleet.id)
        )
    ).scalar_one_or_none()
    if key is None:
        return _coded(404, "not_found", "api key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _jsonable(obj: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(obj)
