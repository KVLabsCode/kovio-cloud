"""V2 audience reporting + playlist editing (migration 010).

Two routers, both fleet-key Bearer auth and hard org-gated:

* ``/campaign/v1/{campaign_id}/audience`` — read-only rollup of
  ``audience_samples`` for a campaign's (single-creative) sessions.
* ``/display/v1/{display_id}/...`` — the same rollup for a display (this is
  where BLENDED looping sessions report: labeled blended, never under any
  campaign), plus playlist item management and the demo-library load-preset.

Attribution honesty rule enforced here: a blended session's samples carry no
campaign_id, so a campaign rollup can never include blended dwell — the two
scopes are disjoint by construction.

Read models + playlist writes only. ``spend_processor``/settlement never touch
(or get touched by) anything in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, require_sdk_auth
from ..db import get_logger, get_session
from ..models import (
    AudienceSample,
    Campaign,
    CustomDisplay,
    CustomDisplayItem,
    DemoCreative,
    Session,
)
from ..schemas import (
    AudienceRollupOut,
    AudienceSessionRow,
    DisplayItemCreateIn,
    DisplayItemOut,
    DisplayItemPatchIn,
    DisplayItemsOut,
    DisplayItemsReorderIn,
    LoadPresetIn,
)

campaign_router = APIRouter(prefix="/campaign/v1", tags=["audience"])
display_router = APIRouter(prefix="/display/v1", tags=["audience"])
log = get_logger("kovio_cloud.audience_v2")


# ------------------------------------------------------------------ helpers --

async def _org_display(
    session: AsyncSession, ctx: AuthContext, display_id: uuid.UUID
) -> CustomDisplay:
    d = (
        await session.execute(
            select(CustomDisplay).where(CustomDisplay.id == display_id)
        )
    ).scalar_one_or_none()
    if d is None or d.org_id != ctx.org_id:
        raise HTTPException(status_code=404, detail="Display not found for this key's org.")
    return d


async def _rollup(
    session: AsyncSession,
    scope: str,
    scope_id: uuid.UUID,
    label: str,
    sessions_filter,
    blended: bool,
    from_ts: datetime | None,
    to_ts: datetime | None,
    creative_count: int | None = None,
) -> AudienceRollupOut:
    """Aggregate audience_samples over the scope's sessions.

    Unique counts are per (session_id, track_id) — track ids are
    session-scoped on the robot, so the pair is the person-encounter key."""

    where = [sessions_filter]
    if from_ts is not None:
        where.append(Session.started_at >= from_ts)
    if to_ts is not None:
        where.append(Session.started_at < to_ts)
    sess_rows = (
        (
            await session.execute(
                select(Session).where(*where).order_by(Session.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    by_id = {s.id: s for s in sess_rows}
    if not by_id:
        return AudienceRollupOut(
            scope=scope, scope_id=scope_id, label=label, blended=blended,
            creative_count=creative_count, from_ts=from_ts, to_ts=to_ts,
            reach_unique=0, passersby_gross=0, dwell_paused_plus=0,
            dwell_engaged_plus=0, dwell_deep=0, close_approaches=0, sessions=[],
        )

    track = tuple_(AudienceSample.session_id, AudienceSample.track_id)
    agg = (
        await session.execute(
            select(
                AudienceSample.session_id,
                func.count(func.distinct(track)).filter(
                    AudienceSample.metric_kind == "passerby"
                ),
                func.count(AudienceSample.id).filter(
                    AudienceSample.metric_kind == "passerby"
                ),
                func.count(func.distinct(track)).filter(
                    AudienceSample.metric_kind == "dwell"
                ),
                func.count(func.distinct(track)).filter(
                    AudienceSample.metric_kind == "dwell",
                    AudienceSample.dwell_tier.in_(("engaged", "deep")),
                ),
                func.count(func.distinct(track)).filter(
                    AudienceSample.metric_kind == "dwell",
                    AudienceSample.dwell_tier == "deep",
                ),
                func.count(AudienceSample.id).filter(
                    AudienceSample.metric_kind == "close_approach"
                ),
            )
            .where(AudienceSample.session_id.in_(by_id.keys()))
            .group_by(AudienceSample.session_id)
        )
    ).all()
    agg_by_session = {r[0]: r[1:] for r in agg}

    rows: list[AudienceSessionRow] = []
    totals = [0, 0, 0, 0, 0, 0]
    for s in sess_rows:
        a = agg_by_session.get(s.id, (0, 0, 0, 0, 0, 0))
        for i in range(6):
            totals[i] += a[i]
        rows.append(
            AudienceSessionRow(
                session_id=s.id,
                started_at=s.started_at,
                ended_at=s.ended_at,
                is_blended=s.is_blended,
                campaign_id=s.campaign_id,
                display_id=s.display_id,
                reach_unique=a[0],
                passersby_gross=a[1],
                dwell_engaged_plus=a[3],
                dwell_deep=a[4],
                close_approaches=a[5],
            )
        )
    return AudienceRollupOut(
        scope=scope, scope_id=scope_id, label=label, blended=blended,
        creative_count=creative_count, from_ts=from_ts, to_ts=to_ts,
        reach_unique=totals[0], passersby_gross=totals[1],
        dwell_paused_plus=totals[2], dwell_engaged_plus=totals[3],
        dwell_deep=totals[4], close_approaches=totals[5], sessions=rows,
    )


# --------------------------------------------------------- campaign rollup --

@campaign_router.get("/{campaign_id}/audience", response_model=AudienceRollupOut)
async def campaign_audience(
    campaign_id: uuid.UUID,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> AudienceRollupOut:
    """Measured audience for one campaign — single-creative sessions only.

    Blended sessions never carry this campaign_id, so their dwell cannot leak
    in. Unique reach counts distinct per-session tracks (dedup'd on-device by
    the encounter cap)."""

    c = (
        await session.execute(select(Campaign).where(Campaign.id == campaign_id))
    ).scalar_one_or_none()
    if c is None or c.org_id != ctx.org_id:
        raise HTTPException(status_code=404, detail="Campaign not found for this key's org.")
    return await _rollup(
        session, "campaign", c.id, c.name,
        Session.campaign_id == c.id, blended=False,
        from_ts=from_ts, to_ts=to_ts,
    )


# ---------------------------------------------------------- display rollup --

@display_router.get("/{display_id}/audience", response_model=AudienceRollupOut)
async def display_audience(
    display_id: uuid.UUID,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> AudienceRollupOut:
    """Measured audience for one display — includes its blended (looping)
    sessions, labeled as such. This is the ONLY place blended numbers roll up;
    they answer "who engaged with the robot", not "who saw advertiser X"."""

    d = await _org_display(session, ctx, display_id)
    items = (
        await session.execute(
            select(func.count(CustomDisplayItem.id)).where(
                CustomDisplayItem.display_id == d.id
            )
        )
    ).scalar_one()
    return await _rollup(
        session, "display", d.id, d.name,
        Session.display_id == d.id, blended=items > 1,
        from_ts=from_ts, to_ts=to_ts, creative_count=items,
    )


# ------------------------------------------------------- playlist editing --

def _item_out(items) -> list[DisplayItemOut]:
    return [DisplayItemOut.model_validate(i) for i in items]


async def _items_of(session: AsyncSession, display_id: uuid.UUID):
    return (
        (
            await session.execute(
                select(CustomDisplayItem)
                .where(CustomDisplayItem.display_id == display_id)
                .order_by(CustomDisplayItem.position, CustomDisplayItem.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _items_payload(
    session: AsyncSession, d: CustomDisplay
) -> DisplayItemsOut:
    return DisplayItemsOut(
        display_id=d.id,
        name=d.name,
        default_image_seconds=d.default_image_seconds,
        items=_item_out(await _items_of(session, d.id)),
    )


@display_router.get("/{display_id}/items", response_model=DisplayItemsOut)
async def list_items(
    display_id: uuid.UUID,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> DisplayItemsOut:
    d = await _org_display(session, ctx, display_id)
    return await _items_payload(session, d)


@display_router.post("/{display_id}/items", response_model=DisplayItemsOut)
async def add_item(
    display_id: uuid.UUID,
    body: DisplayItemCreateIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> DisplayItemsOut:
    d = await _org_display(session, ctx, display_id)
    if body.media_type not in ("image", "video"):
        raise HTTPException(status_code=422, detail="media_type must be image or video.")
    existing = await _items_of(session, d.id)
    next_pos = (max((i.position for i in existing), default=-1)) + 1
    session.add(
        CustomDisplayItem(
            display_id=d.id,
            media_url=body.media_url,
            media_type=body.media_type,
            duration_seconds=body.duration_seconds,
            position=next_pos,
        )
    )
    await session.flush()
    log.info("display_item_added", display_id=str(d.id), position=next_pos)
    return await _items_payload(session, d)


@display_router.patch("/{display_id}/items/{item_id}", response_model=DisplayItemsOut)
async def patch_item(
    display_id: uuid.UUID,
    item_id: uuid.UUID,
    body: DisplayItemPatchIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> DisplayItemsOut:
    d = await _org_display(session, ctx, display_id)
    item = (
        await session.execute(
            select(CustomDisplayItem).where(
                CustomDisplayItem.id == item_id,
                CustomDisplayItem.display_id == d.id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found on this display.")
    item.duration_seconds = body.duration_seconds
    await session.flush()
    return await _items_payload(session, d)


@display_router.delete("/{display_id}/items/{item_id}", response_model=DisplayItemsOut)
async def delete_item(
    display_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> DisplayItemsOut:
    d = await _org_display(session, ctx, display_id)
    await session.execute(
        delete(CustomDisplayItem).where(
            CustomDisplayItem.id == item_id,
            CustomDisplayItem.display_id == d.id,
        )
    )
    # Re-pack positions so the loop order stays dense and predictable.
    for pos, item in enumerate(await _items_of(session, d.id)):
        item.position = pos
    await session.flush()
    log.info("display_item_deleted", display_id=str(d.id), item_id=str(item_id))
    return await _items_payload(session, d)


@display_router.post("/{display_id}/items/reorder", response_model=DisplayItemsOut)
async def reorder_items(
    display_id: uuid.UUID,
    body: DisplayItemsReorderIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> DisplayItemsOut:
    """``item_ids`` is the full new order; must match the display's items."""

    d = await _org_display(session, ctx, display_id)
    items = await _items_of(session, d.id)
    by_id = {i.id: i for i in items}
    if set(body.item_ids) != set(by_id) or len(body.item_ids) != len(items):
        raise HTTPException(
            status_code=409,
            detail="item_ids must be exactly this display's items (stale list?).",
        )
    for pos, iid in enumerate(body.item_ids):
        by_id[iid].position = pos
    await session.flush()
    return await _items_payload(session, d)


# --------------------------------------------------------------- load-preset --

@display_router.post("/{display_id}/load-preset", response_model=DisplayItemsOut)
async def load_preset(
    display_id: uuid.UUID,
    body: LoadPresetIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> DisplayItemsOut:
    """Insert the selected demo creatives as playlist items, in the order
    given, after the existing items. Only the key org's demo set + the global
    (org NULL) set are loadable."""

    d = await _org_display(session, ctx, display_id)
    if not body.creative_ids:
        raise HTTPException(status_code=422, detail="creative_ids is empty.")
    rows = (
        (
            await session.execute(
                select(DemoCreative).where(
                    DemoCreative.id.in_(body.creative_ids),
                    (DemoCreative.org_id == ctx.org_id)
                    | (DemoCreative.org_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in rows}
    missing = [str(c) for c in body.creative_ids if c not in by_id]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"Demo creatives not available: {missing}"
        )
    existing = await _items_of(session, d.id)
    next_pos = (max((i.position for i in existing), default=-1)) + 1
    for offset, cid in enumerate(body.creative_ids):
        c = by_id[cid]
        session.add(
            CustomDisplayItem(
                display_id=d.id,
                media_url=c.media_url,
                media_type=c.media_type,
                duration_seconds=c.default_seconds,
                position=next_pos + offset,
            )
        )
    await session.flush()
    log.info(
        "display_preset_loaded",
        display_id=str(d.id),
        creatives=len(body.creative_ids),
    )
    return await _items_payload(session, d)
