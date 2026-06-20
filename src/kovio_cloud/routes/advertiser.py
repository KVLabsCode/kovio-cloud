"""``/advertiser/v1/*`` — brand-facing endpoints for the advertiser web app.

Auth: every endpoint requires a verified Supabase JWT (human session), NOT an
API key. After verifying the JWT we look up the caller in the ``users`` table to
resolve their ``org_id``; all data is scoped to that org and never crosses the
boundary. A verified user with no ``users`` row is "not onboarded".

No Stripe yet: ``/deposit`` credits the balance directly. When Stripe lands, the
endpoint becomes a Checkout-session creator + webhook handler; the frontend
signature won't change.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audience import audience_summary
from ..db import get_session
from ..models import Campaign, Impression, Organization, Transaction, User
from ..supabase_auth import SupabaseUser, require_supabase_user

router = APIRouter(prefix="/advertiser/v1", tags=["advertiser"])

_MAX_DEPOSIT_CENTS = 1_000_000  # $10K cap to avoid absurd test values


# --- helpers -----------------------------------------------------------------
def _coded(status_code: int, code: str, detail: str) -> JSONResponse:
    """A flat, machine-readable error body: {"code": ..., "detail": ...}."""

    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


def _user_dict(u: User) -> dict[str, Any]:
    return {"id": u.id, "email": u.email, "role": u.role}


def _org_summary(org: Organization) -> dict[str, Any]:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "balance_cents": org.balance_cents,
        "created_at": org.created_at,
    }


def _campaign_dict(c: Campaign) -> dict[str, Any]:
    return {
        "id": c.id,
        "campaign_id": c.campaign_id,
        "name": c.name,
        "advertiser": c.advertiser,
        "creative_url": c.creative_url,
        "targeting": c.targeting,
        "category": c.category,
        "status": c.status,
        "enabled": c.enabled,
        "priority": c.priority,
        "encounter_cap_seconds": c.encounter_cap_seconds,
        "budget_total_cents": c.budget_total_cents,
        "budget_spent_cents": c.budget_spent_cents,
        "cost_per_impression_cents": c.cost_per_impression_cents,
        "cost_per_attended_cents": c.cost_per_attended_cents,
        "cost_per_engagement_cents": c.cost_per_engagement_cents,
        "start_at": c.start_at,
        "end_at": c.end_at,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


async def _lookup_user(supa: SupabaseUser, session: AsyncSession) -> User | None:
    return (
        await session.execute(
            select(User).where(User.supabase_user_id == uuid.UUID(supa.supabase_user_id))
        )
    ).scalar_one_or_none()


async def _org_for(user: User, session: AsyncSession) -> Organization:
    return (
        await session.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one()


# --- request bodies ----------------------------------------------------------
class OnboardingBody(BaseModel):
    org_name: str
    org_slug: str


class CampaignBody(BaseModel):
    campaign_id: str
    name: str
    advertiser: str = ""
    creative_url: str
    targeting: list[dict[str, Any]] = Field(default_factory=list)
    priority: int = 10
    encounter_cap_seconds: int = 300
    category: str | None = None
    budget_total_cents: int
    cost_per_impression_cents: float
    cost_per_attended_cents: float = 5.0
    start_at: datetime
    end_at: datetime | None = None


class DepositBody(BaseModel):
    amount_cents: int


# --- GET /me -----------------------------------------------------------------
@router.get("/me")
async def me(
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return _coded(404, "not_onboarded", "complete onboarding first")
    org = await _org_for(user, session)
    if org.kind != "advertiser":
        return _coded(403, "wrong_user_kind", "this user is an OEM, not an advertiser")
    return {"user": _user_dict(user), "org": _org_summary(org)}


# --- POST /onboarding --------------------------------------------------------
@router.post("/onboarding")
async def onboarding(
    body: OnboardingBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    existing = await _lookup_user(supa, session)
    if existing is not None:
        return _coded(409, "already_onboarded", "this account is already linked to an org")

    # New advertisers get one free campaign (the default setup) — handled at
    # campaign-creation time, not as a prepaid balance. No credits/coins.
    org = Organization(
        name=body.org_name,
        slug=body.org_slug,
        kind="advertiser",
        balance_cents=0,
    )
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

    return {"user": _user_dict(user), "org": _org_summary(org)}


# --- GET /dashboard ----------------------------------------------------------
@router.get("/dashboard")
async def dashboard(
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return _coded(404, "not_onboarded", "complete onboarding first")
    org = await _org_for(user, session)
    org_id = org.id

    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_30d = now - timedelta(days=30)

    async def _scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    total_campaigns = await _scalar(
        select(func.count()).select_from(Campaign).where(Campaign.org_id == org_id)
    )
    active_campaigns = await _scalar(
        select(func.count()).select_from(Campaign).where(
            Campaign.org_id == org_id, Campaign.status == "active"
        )
    )
    paused_campaigns = await _scalar(
        select(func.count()).select_from(Campaign).where(
            Campaign.org_id == org_id, Campaign.status == "paused"
        )
    )
    impressions_24h = await _scalar(
        select(func.count()).select_from(Impression).where(
            Impression.advertiser_org_id == org_id, Impression.created_at >= since_24h
        )
    )
    impressions_30d = await _scalar(
        select(func.count()).select_from(Impression).where(
            Impression.advertiser_org_id == org_id, Impression.created_at >= since_30d
        )
    )
    spent_24h = await _scalar(
        select(func.coalesce(func.sum(Impression.cost_cents), 0)).where(
            Impression.advertiser_org_id == org_id, Impression.created_at >= since_24h
        )
    )
    spent_30d = await _scalar(
        select(func.coalesce(func.sum(Impression.cost_cents), 0)).where(
            Impression.advertiser_org_id == org_id, Impression.created_at >= since_30d
        )
    )

    recent_rows = (
        await session.execute(
            select(Impression.cost_cents, Impression.timestamp, Campaign.campaign_id, Campaign.name)
            .join(Campaign, Campaign.id == Impression.campaign_id)
            .where(Impression.advertiser_org_id == org_id)
            .order_by(Impression.timestamp.desc())
            .limit(10)
        )
    ).all()
    recent = [
        {"campaign_id": r.campaign_id, "campaign_name": r.name,
         "cost_cents": r.cost_cents, "timestamp": r.timestamp}
        for r in recent_rows
    ]

    audience_24h = await audience_summary(
        session, Impression.advertiser_org_id == org_id, Impression.created_at >= since_24h
    )
    audience_30d = await audience_summary(
        session, Impression.advertiser_org_id == org_id, Impression.created_at >= since_30d
    )

    return {
        "balance_cents": org.balance_cents,
        "total_campaigns": total_campaigns,
        "active_campaigns": active_campaigns,
        "paused_campaigns": paused_campaigns,
        "impressions_24h": impressions_24h,
        "impressions_30d": impressions_30d,
        "spent_24h_cents": spent_24h,
        "spent_30d_cents": spent_30d,
        "audience_24h": audience_24h,
        "audience_30d": audience_30d,
        "recent_impressions": recent,
    }


# --- GET /campaigns ----------------------------------------------------------
@router.get("/campaigns")
async def list_campaigns(
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return _coded(404, "not_onboarded", "complete onboarding first")

    rows = (
        await session.execute(
            select(Campaign).where(Campaign.org_id == user.org_id).order_by(Campaign.created_at.desc())
        )
    ).scalars().all()

    # Real per-campaign reach/attention totals from impressions (one grouped pass).
    agg_rows = (
        await session.execute(
            select(
                Impression.campaign_id,
                func.count().label("impressions"),
                func.coalesce(func.sum(Impression.person_count), 0).label("walked"),
                func.coalesce(func.sum(Impression.attended_count), 0).label("attended"),
            )
            .where(Impression.advertiser_org_id == user.org_id)
            .group_by(Impression.campaign_id)
        )
    ).all()
    agg = {r.campaign_id: r for r in agg_rows}

    def _with_stats(c: Campaign) -> dict[str, Any]:
        a = agg.get(c.id)
        impressions = int(a.impressions) if a else 0
        attended = int(a.attended) if a else 0
        return {
            **_campaign_dict(c),
            "impressions_total": impressions,
            "walked_by_total": int(a.walked) if a else 0,
            "attended_total": attended,
            "attention_rate": (attended / impressions) if impressions else 0.0,
        }

    return {"campaigns": [_with_stats(c) for c in rows]}


# --- POST /campaigns ---------------------------------------------------------
@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return _coded(404, "not_onboarded", "complete onboarding first")
    org = await _org_for(user, session)

    if body.budget_total_cents <= 0:
        return _coded(400, "invalid_budget", "budget_total_cents must be > 0")
    if body.cost_per_impression_cents <= 0:
        return _coded(400, "invalid_cost", "cost_per_impression_cents must be > 0")
    # NOTE: balance/payment gating is intentionally disabled until Stripe lands.
    # Campaigns can be created freely so the end-to-end flow is testable; the
    # spend processor (off in dev) is what would otherwise debit a balance.

    campaign = Campaign(
        org_id=org.id,
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
        start_at=body.start_at,
        end_at=body.end_at,
        status="active",
        enabled=True,
    )
    session.add(campaign)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return _coded(409, "campaign_id_taken", "that campaign_id is already in use")

    return JSONResponse(status_code=201, content=_jsonable(_campaign_dict(campaign)))


# --- GET /campaigns/{id} -----------------------------------------------------
@router.get("/campaigns/{campaign_pk}")
async def campaign_detail(
    campaign_pk: uuid.UUID,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return _coded(404, "not_onboarded", "complete onboarding first")

    campaign = (
        await session.execute(select(Campaign).where(Campaign.id == campaign_pk))
    ).scalar_one_or_none()
    if campaign is None or campaign.org_id != user.org_id:
        return _coded(404, "not_found", "campaign not found")

    impressions_total = int(
        (await session.execute(
            select(func.count()).select_from(Impression).where(Impression.campaign_id == campaign.id)
        )).scalar_one()
    )
    spent_total = int(
        (await session.execute(
            select(func.coalesce(func.sum(Impression.cost_cents), 0)).where(
                Impression.campaign_id == campaign.id
            )
        )).scalar_one()
    )
    walked_attended = (
        await session.execute(
            select(
                func.coalesce(func.sum(Impression.person_count), 0).label("walked"),
                func.coalesce(func.sum(Impression.attended_count), 0).label("attended"),
            ).where(Impression.campaign_id == campaign.id)
        )
    ).one()
    walked_by_total = int(walked_attended.walked)
    attended_total = int(walked_attended.attended)

    since_30d = datetime.now(timezone.utc) - timedelta(days=30)
    day = func.date(Impression.timestamp)
    by_day_rows = (
        await session.execute(
            select(
                day.label("day"),
                func.count().label("impressions"),
                func.coalesce(func.sum(Impression.cost_cents), 0).label("spent_cents"),
            )
            .where(Impression.campaign_id == campaign.id, Impression.timestamp >= since_30d)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    by_day = [
        {"date": str(r.day), "impressions": int(r.impressions), "spent_cents": int(r.spent_cents)}
        for r in by_day_rows
    ]

    audience_30d = await audience_summary(
        session, Impression.campaign_id == campaign.id, Impression.timestamp >= since_30d
    )

    return {
        "campaign": _campaign_dict(campaign),
        "stats": {
            "impressions_total": impressions_total,
            "spent_cents_total": spent_total,
            "remaining_cents": campaign.budget_total_cents - campaign.budget_spent_cents,
            "walked_by_total": walked_by_total,
            "attended_total": attended_total,
            "by_day": by_day,
            "audience_30d": audience_30d,
        },
    }


# --- POST /campaigns/{id}/pause ----------------------------------------------
@router.post("/campaigns/{campaign_pk}/pause")
async def pause_campaign(
    campaign_pk: uuid.UUID,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    campaign, err = await _scoped_campaign(campaign_pk, supa, session)
    if err is not None:
        return err
    campaign.status = "paused"
    campaign.enabled = False
    return _campaign_dict(campaign)


# --- POST /campaigns/{id}/resume ---------------------------------------------
@router.post("/campaigns/{campaign_pk}/resume")
async def resume_campaign(
    campaign_pk: uuid.UUID,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    campaign, err = await _scoped_campaign(campaign_pk, supa, session)
    if err is not None:
        return err
    if campaign.budget_spent_cents >= campaign.budget_total_cents:
        return _coded(400, "budget_exhausted", "campaign budget is exhausted; top up first")
    campaign.status = "active"
    campaign.enabled = True
    return _campaign_dict(campaign)


# --- POST /deposit -----------------------------------------------------------
@router.post("/deposit")
async def deposit(
    body: DepositBody,
    supa: SupabaseUser = Depends(require_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return _coded(404, "not_onboarded", "complete onboarding first")
    if body.amount_cents <= 0:
        return _coded(400, "invalid_amount", "amount_cents must be > 0")
    if body.amount_cents > _MAX_DEPOSIT_CENTS:
        return _coded(400, "amount_too_large", f"amount_cents capped at {_MAX_DEPOSIT_CENTS}")

    org = await _org_for(user, session)
    org.balance_cents += body.amount_cents
    session.add(
        Transaction(
            org_id=org.id,
            kind="advertiser_deposit",
            amount_cents=body.amount_cents,
            reference_type="mock_deposit",
            reference_id=None,
            metadata_={"source": "advertiser_portal_mock"},
        )
    )
    await session.flush()
    return {"balance_cents": org.balance_cents}


# --- shared scoped-campaign loader (for pause/resume) ------------------------
async def _scoped_campaign(
    campaign_pk: uuid.UUID, supa: SupabaseUser, session: AsyncSession
) -> tuple[Campaign | None, JSONResponse | None]:
    user = await _lookup_user(supa, session)
    if user is None or user.org_id is None:
        return None, _coded(404, "not_onboarded", "complete onboarding first")
    campaign = (
        await session.execute(select(Campaign).where(Campaign.id == campaign_pk))
    ).scalar_one_or_none()
    if campaign is None or campaign.org_id != user.org_id:
        return None, _coded(404, "not_found", "campaign not found")
    return campaign, None


def _jsonable(obj: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(obj)
