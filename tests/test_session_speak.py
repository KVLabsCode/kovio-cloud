"""POST /session/v1/speak + speak_text surfacing on /current (dashboard TTS).

Calls the route handlers directly against a seeded Postgres (same style as
``test_display_radar``): an open session for a fleet robot, then assert
``speak()`` queues the utterance and ``current_session()`` surfaces it with a
matching nonce; and that ``speak()`` 409s when no session is open.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException


def _ctx(org_id, fleet_id):
    from kovio_cloud.auth import AuthContext

    return AuthContext(
        api_key_id=uuid.uuid4(),
        org_id=org_id,
        org_kind="oem",
        fleet_id=fleet_id,
        scopes=["sdk"],
    )


async def _seed_open_session(session):
    from kovio_cloud.models import Fleet, Organization, Robot, Session

    org = Organization(name="Speak OEM", slug="speak-oem", kind="oem")
    session.add(org)
    await session.flush()
    fleet = Fleet(org_id=org.id, name="Speak Fleet")
    session.add(fleet)
    await session.flush()
    robot = Robot(
        fleet_id=fleet.id,
        external_id="speak-bot-1",
        status="active",
        last_heartbeat=datetime.now(timezone.utc),
    )
    session.add(robot)
    await session.flush()
    sess = Session(
        robot_id=robot.id,
        fleet_id=fleet.id,
        org_id=org.id,
        status="recording",
        started_at=datetime.now(timezone.utc),
    )
    session.add(sess)
    await session.flush()
    return org, fleet, robot, sess


async def test_speak_queues_and_current_surfaces_once(clean_db):
    from kovio_cloud.db import session_scope
    from kovio_cloud.routes.sessions import _PENDING_SPEECH, current_session, speak
    from kovio_cloud.schemas import SessionSpeakIn

    async with session_scope() as session:
        org, fleet, robot, _sess = await _seed_open_session(session)
        ctx = _ctx(org.id, fleet.id)

        out = await speak(
            SessionSpeakIn(robot_id=robot.id, text="hello world", volume=90),
            ctx,
            session,
        )
        assert out.ok is True and out.nonce

        cur = await current_session(robot.external_id, ctx, session)
        assert cur.active is True
        assert cur.speak_text == "hello world"
        assert cur.speak_nonce == out.nonce
        assert cur.speak_volume == 90

        # Latest-wins: a second queue overwrites with a fresh nonce.
        out2 = await speak(
            SessionSpeakIn(robot_id=robot.id, text="again"), ctx, session
        )
        assert out2.nonce != out.nonce
        cur2 = await current_session(robot.external_id, ctx, session)
        assert cur2.speak_text == "again"
        assert cur2.speak_nonce == out2.nonce
        assert cur2.speak_volume is None

    _PENDING_SPEECH.pop(robot.id, None)


async def test_speak_requires_open_session(clean_db):
    from kovio_cloud.db import session_scope
    from kovio_cloud.models import Fleet, Organization, Robot
    from kovio_cloud.routes.sessions import speak
    from kovio_cloud.schemas import SessionSpeakIn

    async with session_scope() as session:
        org = Organization(name="NoSess OEM", slug="nosess-oem", kind="oem")
        session.add(org)
        await session.flush()
        fleet = Fleet(org_id=org.id, name="NoSess Fleet")
        session.add(fleet)
        await session.flush()
        robot = Robot(
            fleet_id=fleet.id,
            external_id="nosess-bot",
            status="active",
            last_heartbeat=datetime.now(timezone.utc),
        )
        session.add(robot)
        await session.flush()
        ctx = _ctx(org.id, fleet.id)

        with pytest.raises(HTTPException) as ei:
            await speak(SessionSpeakIn(robot_id=robot.id, text="hi"), ctx, session)
        assert ei.value.status_code == 409
