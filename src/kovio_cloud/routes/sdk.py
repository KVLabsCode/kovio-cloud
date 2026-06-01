"""``/sdk/v1/*`` — endpoints the robot SDK calls. Bearer auth, ``sdk`` scope."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, require_sdk_auth
from ..db import get_logger, get_session
from ..models import Campaign, EventRaw, Fleet, Robot
from ..schemas import (
    CampaignListResponse,
    CampaignOut,
    EventBatchIn,
    EventBatchResult,
    HeartbeatIn,
    HeartbeatResponse,
)

router = APIRouter(prefix="/sdk/v1", tags=["sdk"])
log = get_logger("kovio_cloud.sdk")


def _require_fleet(ctx: AuthContext):
    if ctx.fleet_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This SDK key is not scoped to a fleet.",
        )
    return ctx.fleet_id


@router.get("/campaigns", response_model=CampaignListResponse)
async def list_campaigns(
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> CampaignListResponse:
    """Active campaigns visible to the requesting fleet.

    Filters: enabled, status='active', budget not exhausted, and the fleet must
    satisfy each campaign's allow/block lists + the fleet's own brand-safety
    blocks. Mirrors what the spend processor would *not* pause.
    """

    fleet_id = _require_fleet(ctx)
    fleet = (
        await session.execute(select(Fleet).where(Fleet.id == fleet_id))
    ).scalar_one_or_none()
    if fleet is None:
        raise HTTPException(status_code=404, detail="Fleet not found for this key.")

    rows = (
        await session.execute(
            select(Campaign).where(
                Campaign.enabled.is_(True),
                Campaign.status == "active",
                Campaign.budget_spent_cents < Campaign.budget_total_cents,
            )
        )
    ).scalars().all()

    blocked_categories = set(fleet.blocked_categories or [])
    blocked_advertisers = set(fleet.blocked_advertisers or [])

    visible: list[CampaignOut] = []
    for c in rows:
        if c.fleet_allowlist and fleet_id not in c.fleet_allowlist:
            continue
        if c.fleet_blocklist and fleet_id in c.fleet_blocklist:
            continue
        if c.category and c.category in blocked_categories:
            continue
        if c.org_id in blocked_advertisers:
            continue
        visible.append(
            CampaignOut(
                campaign_id=c.campaign_id,
                name=c.name,
                advertiser=c.advertiser,
                creative_path=c.creative_url,
                targeting=c.targeting or [],
                priority=c.priority,
                encounter_cap_seconds=c.encounter_cap_seconds,
                enabled=c.enabled,
            )
        )

    return CampaignListResponse(
        campaigns=visible,
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=300,
    )


@router.post("/events/batch", response_model=EventBatchResult)
async def ingest_events(
    body: EventBatchIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> EventBatchResult:
    """Append-only, idempotent event ingest. Costing happens later in the spend
    processor — never inline here."""

    fleet_id = _require_fleet(ctx)
    accepted = duplicates = rejected = 0

    # Cache external_id -> robot UUID lookups within the batch.
    robot_cache: dict[str, object] = {}

    for ev in body.events:
        try:
            external = ev.robot_id
            if external not in robot_cache:
                robot_cache[external] = (
                    await session.execute(
                        select(Robot.id).where(
                            Robot.fleet_id == fleet_id, Robot.external_id == external
                        )
                    )
                ).scalar_one_or_none()
            robot_uuid = robot_cache[external]

            ts = datetime.fromtimestamp(ev.timestamp, tz=timezone.utc)

            stmt = (
                pg_insert(EventRaw)
                .values(
                    event_id=ev.event_id,
                    robot_id=robot_uuid,
                    fleet_id=fleet_id,
                    robot_external_id=external,
                    event_type=ev.event_type,
                    payload=ev.payload,
                    timestamp=ts,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            result = await session.execute(stmt)
            if result.rowcount and result.rowcount > 0:
                accepted += 1
            else:
                duplicates += 1
        except Exception:
            rejected += 1
            log.warning("event_rejected", event_id=str(ev.event_id), exc_info=True)

    return EventBatchResult(accepted=accepted, duplicates=duplicates, rejected=rejected)


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> HeartbeatResponse:
    """Update a robot's last_heartbeat. Auto-registers on first boot."""

    fleet_id = _require_fleet(ctx)
    robot = (
        await session.execute(
            select(Robot).where(
                Robot.fleet_id == fleet_id, Robot.external_id == body.robot_id
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    registered = False
    if robot is None:
        robot = Robot(
            fleet_id=fleet_id,
            external_id=body.robot_id,
            status=body.status,
            last_heartbeat=now,
            meta=body.metadata,
        )
        session.add(robot)
        await session.flush()
        registered = True
        log.info("robot_auto_registered", fleet_id=str(fleet_id), external_id=body.robot_id)
    else:
        robot.last_heartbeat = now
        robot.status = body.status
        if body.metadata:
            robot.meta = {**(robot.meta or {}), **body.metadata}

    return HeartbeatResponse(ok=True, robot_id=robot.id, registered=registered)
