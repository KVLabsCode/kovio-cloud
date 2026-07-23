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

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, require_sdk_auth
from ..config import get_settings
from ..conversation import reply_wav
from ..db import get_logger, get_session
from ..greeting import build_context, render_greeting_wav
from ..models import (
    AudienceSample,
    Campaign,
    CustomDisplay,
    CustomDisplayItem,
    DemoCreative,
    DisplayAssignment,
    EventRaw,
    Impression,
    Robot,
    Session,
)
from ..schemas import (
    DemoCreativeOut,
    MomentsAck,
    MomentsIn,
    SensorHealthOut,
    SessionCampaignOut,
    SessionCurrentOut,
    SessionListenIn,
    SessionListenOut,
    SessionMetricsOut,
    SessionOut,
    SessionRobotOut,
    SessionRobotsResponse,
    SessionSpeakIn,
    SessionSpeakOut,
    SessionStartIn,
    SessionStopIn,
    SessionSummaryOut,
    SessionUtteranceIn,
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

# Latest sensor-health snapshot per robot UUID -> (dict, received_at). Same
# in-RAM posture as _FRAMES: live-panel plumbing, never persisted.
_SENSORS: dict[uuid.UUID, tuple[dict, datetime]] = {}

# Pending dashboard TTS per robot UUID -> (text, nonce, volume, queued_at).
# Same in-RAM posture as _FRAMES/_SENSORS: a single latest utterance per robot,
# surfaced on the robot's /current poll, dropped on stop/restart. The robot
# de-dupes on the nonce, and we only surface utterances younger than
# _SPEECH_TTL_S so a stale command can't replay after the robot reconnects.
_PENDING_SPEECH: dict[uuid.UUID, tuple[str, str, int | None, datetime]] = {}

_SPEECH_TTL_SECONDS = 30

# Pending greeting audio per robot UUID -> (wav_bytes, nonce, text, queued_at).
# Rendered off-request when a session starts (OpenRouter -> ElevenLabs), held in
# process RAM exactly like _FRAMES/_PENDING_SPEECH, fetched once by the robot's
# /current poll and played out its Bluetooth speaker, dropped on stop/restart.
# TTL is generous because rendering takes a second or two before the robot's
# next poll can pick it up; it still can't replay after a reconnect gap.
_PENDING_AUDIO: dict[uuid.UUID, tuple[bytes, str, str, datetime]] = {}

_AUDIO_TTL_SECONDS = 90

# Conversation mode: robot UUID -> last-activity time. Presence means the robot
# should be in CONTINUOUS listen/reply mode — opened by /listen, refreshed on
# each /utterance. Auto-ends after _CONVO_IDLE_TTL of no turns, on explicit
# /conversation/stop, or on session stop. Same in-RAM posture as the rest.
_CONVERSATION: dict[uuid.UUID, datetime] = {}

_CONVO_IDLE_TTL_SECONDS = 180

# Per-session conversation history: session UUID -> [{role, content}, ...].
# Process-RAM only, like every other bit of session live-state; dropped on stop.
_CONVO: dict[uuid.UUID, list[dict]] = {}

_CONVO_MAX_MESSAGES = 24  # hard cap so a marathon chat can't grow unbounded

_MAX_FRAME_BYTES = 2_000_000  # hotspot-friendly cap; ~640x480 JPEGs are ~50KB

_DEFAULT_ENCOUNTER_CAP_S = 300

_MOMENT_KINDS = {"passerby", "dwell", "close_approach"}


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


def _render_greeting_bg(robot_id: uuid.UUID, ctx: dict) -> None:
    """Background task: render the greeting (OpenRouter -> ElevenLabs) and stash
    the WAV for the robot's next /current poll. Runs in FastAPI's threadpool
    (``render_greeting_wav`` is sync ``requests``); total — never raises."""
    settings = get_settings()
    result = render_greeting_wav(ctx, settings)
    if result is None:
        return
    text, wav = result
    _PENDING_AUDIO[robot_id] = (
        wav,
        uuid.uuid4().hex,
        text,
        datetime.now(timezone.utc),
    )
    log.info("greeting_queued", robot=str(robot_id), wav_bytes=len(wav))


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
    background_tasks: BackgroundTasks,
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

    # --- V2 attribution mode, decided by the display's playlist length -------
    # 1 item  -> single-creative: the operator may bind ONE campaign; metrics
    #            attribute to it.
    # >1 item -> looping: BLENDED. Campaign binding is forbidden — blended
    #            dwell must never be reported under a single advertiser.
    item_count = 0
    is_blended = False
    if body.display_id is not None:
        item_count = (
            await session.execute(
                select(func.count(CustomDisplayItem.id)).where(
                    CustomDisplayItem.display_id == body.display_id
                )
            )
        ).scalar_one()
        is_blended = item_count > 1

    campaign = None
    if body.campaign_id is not None:
        if is_blended:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Display loops {item_count} creatives — this session is "
                    "blended and cannot bind a campaign."
                ),
            )
        campaign = (
            await session.execute(
                select(Campaign).where(Campaign.id == body.campaign_id)
            )
        ).scalar_one_or_none()
        # Server-side org gate: never bind another org's campaign, and don't
        # leak whether the id exists.
        if campaign is None or campaign.org_id != ctx.org_id:
            raise HTTPException(
                status_code=404, detail="Campaign not found for this key's org."
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
        campaign_id=campaign.id if campaign is not None else None,
        is_blended=is_blended,
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
        campaign_id=str(campaign.id) if campaign is not None else None,
        is_blended=is_blended,
    )

    # Greeting-on-Go: render a fresh spoken welcome off-request so /start stays
    # snappy. The WAV lands in _PENDING_AUDIO within a second or two and the
    # robot picks it up on its next 5s poll. Feature-flagged; any failure inside
    # the task is swallowed (session start must never depend on it).
    if get_settings().greeting_on_start:
        _PENDING_AUDIO.pop(robot.id, None)  # clear any stale greeting first
        background_tasks.add_task(
            _render_greeting_bg,
            robot.id,
            build_context(
                campaign_name=campaign.name if campaign is not None else None,
                advertiser=campaign.advertiser if campaign is not None else None,
                category=campaign.category if campaign is not None else None,
                is_blended=is_blended,
            ),
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
    _SENSORS.pop(row.robot_id, None)
    _PENDING_SPEECH.pop(row.robot_id, None)
    _PENDING_AUDIO.pop(row.robot_id, None)
    _CONVERSATION.pop(row.robot_id, None)
    _CONVO.pop(row.id, None)
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
    # The robot's tracker de-duplicates re-entries within this window (the
    # bound campaign's encounter cap, else the platform default).
    cap = _DEFAULT_ENCOUNTER_CAP_S
    if row.campaign_id is not None:
        cap = (
            await session.execute(
                select(Campaign.encounter_cap_seconds).where(
                    Campaign.id == row.campaign_id
                )
            )
        ).scalar_one_or_none() or cap
    speak_text = speak_nonce = speak_audio_url = None
    speak_volume = None

    # Greeting audio (rendered voice) takes precedence over dashboard text TTS:
    # when a greeting is pending we hand the robot a URL to fetch+play the WAV
    # out its Bluetooth speaker instead of the onboard robotic TTS.
    audio = _PENDING_AUDIO.get(robot.id)
    if audio is not None:
        _wav, a_nonce, _text, a_queued = audio
        if (datetime.now(timezone.utc) - a_queued).total_seconds() <= _AUDIO_TTL_SECONDS:
            speak_nonce = a_nonce
            speak_audio_url = (
                f"/session/v1/speak-audio?robot_id={robot.external_id}"
                f"&nonce={a_nonce}"
            )
        else:
            _PENDING_AUDIO.pop(robot.id, None)

    if speak_audio_url is None:
        pending = _PENDING_SPEECH.get(robot.id)
        if pending is not None:
            text, nonce, volume, queued_at = pending
            age = (datetime.now(timezone.utc) - queued_at).total_seconds()
            if age <= _SPEECH_TTL_SECONDS:
                speak_text, speak_nonce, speak_volume = text, nonce, volume
            else:
                # Expired before the robot picked it up (offline/restart): drop
                # it so it can never replay on reconnect.
                _PENDING_SPEECH.pop(robot.id, None)

    # Conversation mode: tell the robot whether to be in continuous listen/reply
    # mode. Auto-expires after idle TTL so a forgotten session can't listen
    # forever.
    conversation_active = False
    last = _CONVERSATION.get(robot.id)
    if last is not None:
        if (datetime.now(timezone.utc) - last).total_seconds() <= _CONVO_IDLE_TTL_SECONDS:
            conversation_active = True
        else:
            _CONVERSATION.pop(robot.id, None)

    return SessionCurrentOut(
        active=True,
        session_id=row.id,
        started_at=row.started_at,
        encounter_cap_seconds=cap,
        speak_text=speak_text,
        speak_nonce=speak_nonce,
        speak_volume=speak_volume,
        speak_audio_url=speak_audio_url,
        conversation_active=conversation_active,
    )


# --- POST /listen — enter continuous conversation mode --------------------------
@router.post("/listen", response_model=SessionListenOut)
async def listen(
    body: SessionListenIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionListenOut:
    """Enter CONTINUOUS conversation mode: the robot listens, replies, and
    re-listens automatically — no need to re-press per turn — until
    /conversation/stop, session stop, or the idle timeout. Requires an open
    session. Starts a fresh conversation history."""

    fleet_id = _require_fleet(ctx)
    robot = await _fleet_robot(session, fleet_id, body.robot_id)
    open_session = await _open_session_for_robot(session, robot.id)
    if open_session is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No open session for this robot — start one before talking.",
        )
    _CONVERSATION[robot.id] = datetime.now(timezone.utc)
    _CONVO.pop(open_session.id, None)  # fresh conversation each time you start
    log.info("conversation_started", session_id=str(open_session.id), robot=robot.external_id)
    return SessionListenOut(ok=True, nonce="conversation")


# --- POST /conversation/stop — leave conversation mode --------------------------
@router.post("/conversation/stop", response_model=SessionListenOut)
async def conversation_stop(
    body: SessionListenIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionListenOut:
    """End continuous conversation mode for a robot (the dashboard 'End'
    button). The robot's next poll sees conversation_active=False and stops
    after its current turn."""

    fleet_id = _require_fleet(ctx)
    robot = await _fleet_robot(session, fleet_id, body.robot_id)
    _CONVERSATION.pop(robot.id, None)
    log.info("conversation_stopped", robot=robot.external_id)
    return SessionListenOut(ok=True, nonce="stopped")


# --- POST /utterance — robot uploads recognized speech, gets a spoken reply -----
@router.post("/utterance")
async def utterance(
    body: SessionUtteranceIn,
    robot_id: str,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The robot's locally-transcribed speech. We append it to the session's
    conversation, ask the LLM for a reply, render it with ElevenLabs, and return
    the reply WAV directly (audio/wav) so the robot can play it immediately —
    no extra poll round-trip. The reply text rides in the ``X-Reply-Text``
    header. Requires an open session; 409 tells the robot to stop."""

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

    # Conversation continues — refresh the activity clock so mode stays alive.
    if robot.id in _CONVERSATION:
        _CONVERSATION[robot.id] = datetime.now(timezone.utc)

    history = _CONVO.setdefault(open_session.id, [])
    history.append({"role": "user", "content": body.text})
    log.info(
        "session_utterance",
        session_id=str(open_session.id),
        robot=robot.external_id,
        chars=len(body.text),
    )

    settings = get_settings()
    result = await run_in_threadpool(reply_wav, list(history), settings)
    if result is None:
        # No reply (LLM/TTS off or errored): keep the user turn but don't leave a
        # dangling assistant turn; tell the robot there's nothing to play.
        raise HTTPException(status_code=503, detail="Reply generation unavailable.")

    reply_text, wav = result
    history.append({"role": "assistant", "content": reply_text})
    # Trim to the cap (keep the most recent turns).
    if len(history) > _CONVO_MAX_MESSAGES:
        del history[: len(history) - _CONVO_MAX_MESSAGES]

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Reply-Text": reply_text.encode("ascii", "replace").decode("ascii"),
        },
    )


# --- GET /speak-audio — the robot fetches its pending greeting WAV ---------------
@router.get("/speak-audio")
async def get_speak_audio(
    robot_id: str,
    nonce: str,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the pending greeting WAV for a robot (by external_id + nonce).

    Same fleet-key auth and in-RAM posture as /frame: the bytes live only in
    ``_PENDING_AUDIO`` and are fetched once by the robot on the poll that saw
    ``speak_audio_url``. The nonce must match so a stale URL can't pull a newer
    greeting."""

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
    entry = _PENDING_AUDIO.get(robot.id)
    if entry is None or entry[1] != nonce:
        raise HTTPException(status_code=404, detail="No pending greeting for this nonce.")
    wav, _nonce, _text, _queued = entry
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


# --- POST /speak — admin queues TTS for a robot's open session -------------------
@router.post("/speak", response_model=SessionSpeakOut)
async def speak(
    body: SessionSpeakIn,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionSpeakOut:
    """Queue a line of text for the robot to speak. Requires an open session
    (the robot only polls /current — hence only receives speech — while live),
    which also scopes the feature to "we're live showing campaigns". The text
    is held in process RAM (latest wins per robot) and handed to the robot on
    its next /current poll; ~5s worst-case latency. Same ``sdk`` fleet-key auth
    the admin panel already uses for start/stop."""

    fleet_id = _require_fleet(ctx)
    robot = await _fleet_robot(session, fleet_id, body.robot_id)
    open_session = await _open_session_for_robot(session, robot.id)
    if open_session is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No open session for this robot — start one before speaking.",
        )

    nonce = uuid.uuid4().hex
    _PENDING_SPEECH[robot.id] = (
        body.text,
        nonce,
        body.volume,
        datetime.now(timezone.utc),
    )
    log.info(
        "session_speak_queued",
        session_id=str(open_session.id),
        robot=robot.external_id,
        chars=len(body.text),
    )
    return SessionSpeakOut(ok=True, nonce=nonce)


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


# --- GET /campaigns — the Start-time campaign picker ------------------------------
@router.get("/campaigns", response_model=list[SessionCampaignOut])
async def list_campaigns(
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> list[SessionCampaignOut]:
    """ONLY the key org's campaigns — the org gate that keeps one org's robot
    metrics from ever landing on another org's campaign (Coca-Cola exists 3×
    in prod; an unfiltered picker would leak across them)."""

    _require_fleet(ctx)
    rows = (
        await session.execute(
            select(Campaign)
            .where(
                Campaign.org_id == ctx.org_id,
                Campaign.enabled.is_(True),
                Campaign.status.in_(("active", "paused")),
            )
            .order_by(Campaign.created_at.desc())
        )
    ).scalars().all()
    return [SessionCampaignOut.model_validate(r) for r in rows]


# --- POST /moments — the robot's V2 audience-metric uplink ------------------------
@router.post("/moments", response_model=MomentsAck)
async def post_moments(
    body: MomentsIn,
    robot_id: str,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> MomentsAck:
    """Insert audience moments as ``audience_samples`` rows.

    Everything sensitive is stamped SERVER-side from the open session and the
    auth context — session/campaign/org ids in the client body would be
    ignored. ``event_id`` (the robot's moment uuid, UNIQUE) makes retried
    uploads idempotent. Rejected with 409 when no session is open, which is
    also the robot's signal to stop posting. Insight-only: this writes only
    ``audience_samples``; spend/settlement never read it."""

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

    if body.sensor is not None:
        _SENSORS[robot.id] = (
            body.sensor.model_dump(), datetime.now(timezone.utc)
        )

    # campaign_id rides on samples ONLY in single-creative mode (never blended).
    campaign_id = None if open_session.is_blended else open_session.campaign_id

    accepted = duplicates = 0
    for m in body.moments:
        if m.kind not in _MOMENT_KINDS:
            log.warning("moment_unknown_kind", kind=m.kind)
            continue
        nearest = m.closest_m if m.closest_m is not None else (m.min_m or 0)
        values = dict(
            event_id=m.moment_id,
            moment_id=str(m.moment_id),
            session_id=open_session.id,
            campaign_id=campaign_id,
            advertiser_org_id=(ctx.org_id if campaign_id is not None else None),
            oem_org_id=ctx.org_id,
            fleet_id=fleet_id,
            robot_id=robot.id,
            metric_kind=m.kind,
            track_id=m.track_id,
            dwell_tier=m.tier if m.kind == "dwell" else None,
            camera_confirmed=m.camera_confirmed,
            lidar_confirmed=m.lidar_confirmed,
            reach=1 if m.kind == "passerby" else 0,
            attended=1 if m.kind in ("dwell", "close_approach") else 0,
            nearest_m=round(nearest, 3),
            dwell_s=round(m.dwell_s if m.dwell_s is not None else (m.duration_s or 0), 2),
            timestamp=datetime.fromtimestamp(m.t, tz=timezone.utc),
        )
        result = await session.execute(
            pg_insert(AudienceSample)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        if result.rowcount:
            accepted += 1
        else:
            duplicates += 1
    if accepted:
        log.info(
            "moments_ingested",
            session_id=str(open_session.id),
            accepted=accepted,
            duplicates=duplicates,
        )
    return MomentsAck(accepted=accepted, duplicates=duplicates)


def _sensor_out(robot_id: uuid.UUID) -> SensorHealthOut | None:
    entry = _SENSORS.get(robot_id)
    if entry is None:
        return None
    snap, received = entry
    age = (datetime.now(timezone.utc) - received).total_seconds()
    stale = age > 30
    return SensorHealthOut(
        lidar_ok=bool(snap.get("lidar_ok")) and not stale,
        lidar_hz=float(snap.get("lidar_hz") or 0) if not stale else 0.0,
        depth_ok=bool(snap.get("depth_ok")) and not stale,
        age_seconds=round(age, 1),
    )


# --- GET /metrics — the V2 live tiles ---------------------------------------------
@router.get("/metrics", response_model=SessionMetricsOut)
async def session_metrics(
    session_id: uuid.UUID,
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> SessionMetricsOut:
    """Unique-track rollup of this session's audience_samples + sensor health.

    Reach counts DISTINCT track_ids (the de-dup V1 lacked); dwell tiers count
    the deepest tier each track reached; ``degraded`` flags a LiDAR that is
    not delivering so the panel never shows a dead sensor as "no audience"."""

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

    counts = (
        await session.execute(
            select(
                func.count(func.distinct(AudienceSample.track_id)).filter(
                    AudienceSample.metric_kind == "passerby"
                ),
                func.count(AudienceSample.id).filter(
                    AudienceSample.metric_kind == "passerby"
                ),
                func.count(func.distinct(AudienceSample.track_id)).filter(
                    AudienceSample.metric_kind == "dwell"
                ),
                func.count(func.distinct(AudienceSample.track_id)).filter(
                    AudienceSample.metric_kind == "dwell",
                    AudienceSample.dwell_tier.in_(("engaged", "deep")),
                ),
                func.count(func.distinct(AudienceSample.track_id)).filter(
                    AudienceSample.metric_kind == "dwell",
                    AudienceSample.dwell_tier == "deep",
                ),
                func.count(AudienceSample.id).filter(
                    AudienceSample.metric_kind == "close_approach"
                ),
            ).where(AudienceSample.session_id == row.id)
        )
    ).one()

    sensor = _sensor_out(row.robot_id) if row.ended_at is None else None
    degraded = row.ended_at is None and (sensor is None or not sensor.lidar_ok)

    return SessionMetricsOut(
        session_id=row.id,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        is_blended=row.is_blended,
        campaign_id=row.campaign_id,
        reach_unique=counts[0],
        passersby_gross=counts[1],
        dwell_paused_plus=counts[2],
        dwell_engaged_plus=counts[3],
        dwell_deep=counts[4],
        close_approaches=counts[5],
        sensor=sensor,
        degraded=degraded,
    )


# --- GET /demo-creatives — the org's demo library ---------------------------------
@router.get("/demo-creatives", response_model=list[DemoCreativeOut])
async def list_demo_creatives(
    ctx: AuthContext = Depends(require_sdk_auth),
    session: AsyncSession = Depends(get_session),
) -> list[DemoCreativeOut]:
    """The key org's demo creatives plus the global (org_id NULL) Kovio set.
    Never another org's."""

    _require_fleet(ctx)
    rows = (
        await session.execute(
            select(DemoCreative)
            .where(
                (DemoCreative.org_id == ctx.org_id) | (DemoCreative.org_id.is_(None))
            )
            .order_by(DemoCreative.org_id.is_(None), DemoCreative.label)
        )
    ).scalars().all()
    return [DemoCreativeOut.model_validate(r) for r in rows]
