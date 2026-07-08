"""``/session/v1/*`` — admin live-view sessions (migration 009).

A session is a start/stop window binding one robot (and the custom display it
is playing) so the admin dashboard can watch the live camera and count the
impressions accruing in that window. Everything here is fleet-key Bearer auth
(same ``sdk`` scope the robot already uses): the admin panel holds the fleet
key, the robot holds the same key, and both talk to this router.

Frames are an in-RAM relay only — the latest JPEG per robot lives in a process
dict, is never written to Postgres or storage, and is dropped on stop. Summary
reads are read-only timestamp-range queries over the existing
events_raw/impressions tables; the spend processor and settlement math are
never touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, require_sdk_auth
from ..db import get_logger, get_session
from ..models import (
    CustomDisplay,
    DisplayAssignment,
    EventRaw,
    Impression,
    Robot,
    Session,
)
from ..schemas import (
    SessionCurrentOut,
    SessionOut,
    SessionRobotOut,
    SessionRobotsResponse,
    SessionStartIn,
    SessionStopIn,
    SessionSummaryOut,
)

router = APIRouter(prefix="/session/v1", tags=["sessions"])
log = get_logger("kovio_cloud.sessions")

# A robot is ONLINE when its last_heartbeat is fresher than this. The SDK's
# CloudSink heartbeats every ~30s flush, so 90s = 3 missed beats. A 15s rule
# would flap offline between beats — don't tighten without changing the SDK.
ONLINE_THRESHOLD_SECONDS = 90

# Latest JPEG per robot UUID -> (bytes, received_at). Process-RAM only; fine on
# the single-machine kovio-api deployment, discarded on stop/restart.
_FRAMES: dict[uuid.UUID, tuple[bytes, datetime]] = {}

_MAX_FRAME_BYTES = 2_000_000  # hotspot-friendly cap; ~640x480 JPEGs are ~50KB


def _require_fleet(ctx: AuthContext) -> uuid.UUID:
    if ctx.fleet_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This SDK key is not scoped to a fleet.",
        )
    return ctx.fleet_id


def _is_online(last_heartbeat: datetime | None, now: datetime) -> bool:
    if last_heartbeat is None:
        return False
    hb = last_heartbeat
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (now - hb).total_seconds() <= ONLINE_THRESHOLD_SECONDS


async def _fleet_robot(
    session: AsyncSession, fleet_id: uuid.UUID, robot_id: uuid.UUID
) -> Robot:
    robot = (
        await session.execute(
            select(Robot).where(Robot.id == robot_id, Robot.fleet_id == fleet_id)
        )
    ).scalar_one_or_none()
    if robot is None:
        raise HTTPException(status_code=404, detail="Robot not found in this key's fleet.")
    return robot


async def _open_session_for_robot(
    session: AsyncSession, robot_id: uuid.UUID
) -> Session | None:
    return (
        await session.execute(
            select(Session).where(
                Session.robot_id == robot_id, Session.ended_at.is_(None)
            )
        )
    ).scalar_one_or_none()


# --- GET /robots — powers the admin robot picker -------------------------------
@router.get("/robots", response_model=SessionRobotsResponse)
async def list_robots(
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionRobotsResponse:
    """The key's fleet's robots with a computed ONLINE flag (90s heartbeat rule)."""

    fleet_id = _require_fleet(ctx)
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(Robot).where(Robot.fleet_id == fleet_id).order_by(Robot.external_id)
        )
    ).scalars().all()
    return SessionRobotsResponse(
        robots=[
            SessionRobotOut(
                id=r.id,
                external_id=r.external_id,
                status=r.status,
                last_heartbeat=r.last_heartbeat,
                online=_is_online(r.last_heartbeat, now),
            )
            for r in rows
        ],
        online_threshold_seconds=ONLINE_THRESHOLD_SECONDS,
    )


# --- POST /start ----------------------------------------------------------------
@router.post("/start", response_model=SessionOut)
async def start_session(
    body: SessionStartIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionOut:
    """Open a session for one online robot; optionally (re)point it at a custom
    display via the existing close-then-open ``display_assignments`` protocol."""

    fleet_id = _require_fleet(ctx)
    robot = await _fleet_robot(session, fleet_id, body.robot_id)

    now = datetime.now(timezone.utc)
    if not _is_online(robot.last_heartbeat, now):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Robot is offline (no heartbeat in {ONLINE_THRESHOLD_SECONDS}s).",
        )

    if body.display_id is not None:
        display = (
            await session.execute(
                select(CustomDisplay).where(CustomDisplay.id == body.display_id)
            )
        ).scalar_one_or_none()
        if display is None or display.org_id != ctx.org_id:
            raise HTTPException(
                status_code=404, detail="Display not found for this key's org."
            )
        # Same close-then-open the OEM assign handler uses, preserving the
        # one-open-assignment-per-robot invariant.
        already_open = (
            await session.execute(
                select(DisplayAssignment).where(
                    DisplayAssignment.robot_id == robot.id,
                    DisplayAssignment.display_id == display.id,
                    DisplayAssignment.effective_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if already_open is None:
            await session.execute(
                update(DisplayAssignment)
                .where(
                    DisplayAssignment.robot_id == robot.id,
                    DisplayAssignment.effective_to.is_(None),
                )
                .values(effective_to=now)
            )
            session.add(
                DisplayAssignment(
                    display_id=display.id, robot_id=robot.id, effective_from=now
                )
            )
            await session.flush()

    # One open session per robot: close any stale one, then open the new window.
    await session.execute(
        update(Session)
        .where(Session.robot_id == robot.id, Session.ended_at.is_(None))
        .values(ended_at=now, status="stopped")
    )
    await session.flush()
    row = Session(
        robot_id=robot.id,
        fleet_id=fleet_id,
        org_id=ctx.org_id,
        display_id=body.display_id,
        status="recording",
        started_at=now,
    )
    session.add(row)
    await session.flush()
    log.info(
        "session_started",
        session_id=str(row.id),
        robot=robot.external_id,
        display_id=str(body.display_id) if body.display_id else None,
    )
    return SessionOut.model_validate(row)


# --- POST /stop -----------------------------------------------------------------
@router.post("/stop", response_model=SessionOut)
async def stop_session(
    body: SessionStopIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionOut:
    """Close the open session (by session_id or robot_id) and drop its frame."""

    fleet_id = _require_fleet(ctx)
    if body.session_id is not None:
        row = (
            await session.execute(
                select(Session).where(
                    Session.id == body.session_id, Session.fleet_id == fleet_id
                )
            )
        ).scalar_one_or_none()
    elif body.robot_id is not None:
        await _fleet_robot(session, fleet_id, body.robot_id)
        row = await _open_session_for_robot(session, body.robot_id)
    else:
        raise HTTPException(status_code=422, detail="Provide session_id or robot_id.")

    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if row.ended_at is None:
        row.ended_at = datetime.now(timezone.utc)
        row.status = "stopped"
        await session.flush()
    _FRAMES.pop(row.robot_id, None)
    log.info("session_stopped", session_id=str(row.id))
    return SessionOut.model_validate(row)


# --- GET /current — the robot's 5s poll ------------------------------------------
@router.get("/current", response_model=SessionCurrentOut)
async def current_session(
    robot_id: str,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionCurrentOut:
    """Is there an open session for this robot? ``robot_id`` is the robot's
    external_id — the same identifier the SDK already uses on every call."""

    fleet_id = _require_fleet(ctx)
    robot = (
        await session.execute(
            select(Robot).where(
                Robot.fleet_id == fleet_id, Robot.external_id == robot_id
            )
        )
    ).scalar_one_or_none()
    if robot is None:
        return SessionCurrentOut(active=False)
    row = await _open_session_for_robot(session, robot.id)
    if row is None:
        return SessionCurrentOut(active=False)
    return SessionCurrentOut(active=True, session_id=row.id, started_at=row.started_at)


# --- POST /frame — robot uploads the latest JPEG ---------------------------------
@router.post("/frame")
async def post_frame(
    request: Request,
    robot_id: str,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
):
    """Raw ``image/jpeg`` body. Kept only in process RAM (latest frame wins);
    rejected when no session is open so the robot stops posting."""

    fleet_id = _require_fleet(ctx)
    robot = (
        await session.execute(
            select(Robot).where(
                Robot.fleet_id == fleet_id, Robot.external_id == robot_id
            )
        )
    ).scalar_one_or_none()
    if robot is None:
        raise HTTPException(status_code=404, detail="Unknown robot.")
    open_session = await _open_session_for_robot(session, robot.id)
    if open_session is None:
        raise HTTPException(status_code=409, detail="No open session for this robot.")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="Empty frame body.")
    if len(body) > _MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="Frame too large.")

    _FRAMES[robot.id] = (body, datetime.now(timezone.utc))
    return {"ok": True, "bytes": len(body)}


# --- GET /frame — the admin page's <img> -----------------------------------------
@router.get("/frame")
async def get_frame(
    robot_id: uuid.UUID,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Latest JPEG for a robot (UUID, as returned by GET /robots)."""

    fleet_id = _require_fleet(ctx)
    await _fleet_robot(session, fleet_id, robot_id)
    entry = _FRAMES.get(robot_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No frame yet.")
    data, received_at = entry
    age = (datetime.now(timezone.utc) - received_at).total_seconds()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Age-Seconds": f"{age:.1f}",
        },
    )


# --- GET /summary — the admin counters -------------------------------------------
@router.get("/summary", response_model=SessionSummaryOut)
async def session_summary(
    session_id: uuid.UUID,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionSummaryOut:
    """Read-only rollup of the session window over the EXISTING impressions
    pipeline (thin ad_played -> spend processor). Nothing is written."""

    fleet_id = _require_fleet(ctx)
    row = (
        await session.execute(
            select(Session).where(
                Session.id == session_id, Session.fleet_id == fleet_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    window_end = row.ended_at or datetime.now(timezone.utc)

    imp = (
        await session.execute(
            select(
                func.count(Impression.id),
                func.coalesce(func.sum(Impression.person_count), 0),
                func.coalesce(func.sum(Impression.attended_count), 0),
            ).where(
                Impression.robot_id == row.robot_id,
                Impression.timestamp >= row.started_at,
                Impression.timestamp < window_end,
            )
        )
    ).one()

    latest_campaign = (
        await session.execute(
            select(EventRaw.payload)
            .where(
                EventRaw.robot_id == row.robot_id,
                EventRaw.event_type == "ad_played",
                EventRaw.timestamp >= row.started_at,
                EventRaw.timestamp < window_end,
            )
            .order_by(EventRaw.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    frame = _FRAMES.get(row.robot_id)
    frame_age = (
        (datetime.now(timezone.utc) - frame[1]).total_seconds() if frame else None
    )

    return SessionSummaryOut(
        session_id=row.id,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        impressions=imp[0],
        person_count=imp[1],
        attended_count=imp[2],
        latest_campaign=(latest_campaign or {}).get("campaign_id"),
        last_frame_age_seconds=frame_age,
    )
