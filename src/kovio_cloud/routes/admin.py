"""``/admin/v1/*`` — internal Kovio team endpoints. Bearer auth, ``admin`` scope."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, generate_api_key, require_admin_auth
from ..db import get_session
from ..models import (
    ApiKey,
    Campaign,
    EventRaw,
    Fleet,
    Impression,
    Organization,
    Robot,
)
from ..schemas import (
    ApiKeyCreate,
    ApiKeyOut,
    CampaignAdminOut,
    CampaignCreate,
    FleetCreate,
    FleetOut,
    OrgCreate,
    OrgOut,
    RobotCreate,
    RobotOut,
    SpendRunResult,
    StatsSummary,
)
from ..spend_processor import run_once

router = APIRouter(prefix="/admin/v1", tags=["admin"])


# --- Organizations ------------------------------------------------------------
@router.post("/orgs", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: OrgCreate,
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
) -> Organization:
    org = Organization(
        name=body.name, slug=body.slug, kind=body.kind, balance_cents=body.balance_cents
    )
    session.add(org)
    await session.flush()
    return org


@router.get("/orgs", response_model=list[OrgOut])
async def list_orgs(
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
):
    return (await session.execute(select(Organization))).scalars().all()


# --- Fleets -------------------------------------------------------------------
@router.post("/fleets", response_model=FleetOut, status_code=status.HTTP_201_CREATED)
async def create_fleet(
    body: FleetCreate,
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
) -> Fleet:
    fleet = Fleet(
        org_id=body.org_id,
        name=body.name,
        region=body.region,
        blocked_categories=body.blocked_categories,
        blocked_advertisers=body.blocked_advertisers,
        revenue_share_pct=body.revenue_share_pct,
    )
    session.add(fleet)
    await session.flush()
    return fleet


@router.get("/fleets", response_model=list[FleetOut])
async def list_fleets(
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
):
    return (await session.execute(select(Fleet))).scalars().all()


# --- Robots -------------------------------------------------------------------
@router.post("/robots", response_model=RobotOut, status_code=status.HTTP_201_CREATED)
async def create_robot(
    body: RobotCreate,
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
) -> Robot:
    robot = Robot(
        fleet_id=body.fleet_id,
        external_id=body.external_id,
        status=body.status,
        meta=body.meta,
    )
    session.add(robot)
    await session.flush()
    return robot


@router.get("/robots", response_model=list[RobotOut])
async def list_robots(
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
):
    return (await session.execute(select(Robot))).scalars().all()


# --- API keys -----------------------------------------------------------------
@router.post("/api-keys", response_model=ApiKeyOut, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyOut:
    full, prefix, key_hash = generate_api_key()
    key = ApiKey(
        org_id=body.org_id,
        fleet_id=body.fleet_id,
        name=body.name,
        key_prefix=prefix,
        key_hash=key_hash,
        scopes=body.scopes,
    )
    session.add(key)
    await session.flush()
    out = ApiKeyOut.model_validate(key)
    out.api_key = full  # plaintext returned exactly once
    return out


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
):
    return (await session.execute(select(ApiKey))).scalars().all()


# --- Campaigns ----------------------------------------------------------------
@router.post("/campaigns", response_model=CampaignAdminOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
) -> Campaign:
    campaign = Campaign(
        org_id=body.org_id,
        campaign_id=body.campaign_id,
        name=body.name,
        advertiser=body.advertiser,
        creative_url=body.creative_url,
        targeting=body.targeting,
        priority=body.priority,
        encounter_cap_seconds=body.encounter_cap_seconds,
        category=body.category,
        budget_total_cents=body.budget_total_cents,
        cost_per_impression_cents=body.cost_per_impression_cents,
        cost_per_attended_cents=body.cost_per_attended_cents,
        cost_per_engagement_cents=body.cost_per_engagement_cents,
    )
    session.add(campaign)
    await session.flush()
    return campaign


@router.get("/campaigns", response_model=list[CampaignAdminOut])
async def list_campaigns_admin(
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
):
    return (await session.execute(select(Campaign))).scalars().all()


# --- Stats --------------------------------------------------------------------
@router.get("/stats/summary", response_model=StatsSummary)
async def stats_summary(
    _: AuthContext = Depends(require_admin_auth),
    session: AsyncSession = Depends(get_session),
) -> StatsSummary:
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    async def _count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return int((await session.execute(stmt)).scalar_one())

    total_spent = (
        await session.execute(
            select(func.coalesce(func.sum(Impression.cost_cents), 0)).where(
                Impression.created_at >= since
            )
        )
    ).scalar_one()
    total_pending = (
        await session.execute(
            select(func.coalesce(func.sum(Organization.pending_payout_cents), 0))
        )
    ).scalar_one()

    return StatsSummary(
        organizations=await _count(Organization),
        fleets=await _count(Fleet),
        robots=await _count(Robot),
        campaigns=await _count(Campaign),
        events_24h=await _count(EventRaw, EventRaw.received_at >= since),
        impressions_24h=await _count(Impression, Impression.created_at >= since),
        total_spent_24h_cents=int(total_spent),
        total_pending_payouts_cents=int(total_pending),
    )


# --- Force a spend run --------------------------------------------------------
@router.post("/process-spend", response_model=SpendRunResult)
async def force_process_spend(
    _: AuthContext = Depends(require_admin_auth),
) -> SpendRunResult:
    """Run one synchronous spend pass. Handy for tests and ops.

    Uses its OWN session (run_once) rather than the request session: FastAPI
    caches Depends(get_session), so the request session already has an open
    transaction from auth, which would collide with the processor's per-event
    transactions.
    """

    result = await run_once()
    return SpendRunResult(**result)
