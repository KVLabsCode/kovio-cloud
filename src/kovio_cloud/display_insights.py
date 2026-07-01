"""On-the-fly interaction metrics for OEM custom displays.

Custom displays carry no money and never reach the spend processor or the
``impressions`` table. Instead, the perception events the robot already streams
(``scene_observed`` / ``interaction_observed`` on ``events_raw``, each stamped
with ``robot_id`` + ``timestamp``) are attributed to a display by JOINING on the
``display_assignments`` history: an event belongs to display D when its robot was
assigned to D and its timestamp falls inside that assignment's half-open
``[effective_from, effective_to)`` interval.

A robot shows exactly one display at a time (the partial-unique invariant in
migration 008), and a display's intervals for a given robot are disjoint, so
every event is attributed to at most one assignment — no double counting.

The summary is shaped identically to ``audience.audience_summary`` (see kovio-web
``lib/types.ts`` ``AudienceSummary``) so the frontend swap from the synthetic
Hawkeye is a drop-in. The perception fields live in ``events_raw.payload`` JSONB,
so this reads JSON keys rather than the Impression columns.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, Numeric, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DisplayAssignment, EventRaw


def _attributed_to(display_id: uuid.UUID, start: datetime, end: datetime):
    """A JOIN condition selecting ``events_raw`` rows attributed to ``display_id``
    within ``[start, end)``. Join target is ``display_assignments``."""
    return and_(
        EventRaw.robot_id == DisplayAssignment.robot_id,
        DisplayAssignment.display_id == display_id,
        EventRaw.timestamp >= DisplayAssignment.effective_from,
        or_(
            DisplayAssignment.effective_to.is_(None),
            EventRaw.timestamp < DisplayAssignment.effective_to,
        ),
        EventRaw.timestamp >= start,
        EventRaw.timestamp < end,
    )


def _num(key: str):
    """``payload->>key`` cast to numeric (NULL when the key is absent)."""
    return cast(EventRaw.payload[key].astext, Numeric)


def _int(key: str):
    return cast(EventRaw.payload[key].astext, Integer)


async def display_summary(
    session: AsyncSession, display_id: uuid.UUID, start: datetime, end: datetime
) -> dict[str, Any]:
    """Aggregate the attributed scene/interaction events into an AudienceSummary."""

    join = EventRaw.__table__.join(
        DisplayAssignment.__table__, _attributed_to(display_id, start, end)
    )

    # --- scene_observed: reach / attention / dwell / crowd / proximity --------
    scene_row = (
        await session.execute(
            select(
                func.count().label("samples"),
                func.coalesce(func.avg(_num("person_count")), 0).label("avg_reach"),
                func.coalesce(func.max(_int("person_count")), 0).label("peak_reach"),
                func.coalesce(func.avg(_num("attended_count")), 0).label("avg_attended"),
                func.least(
                    func.min(_num("mean_distance_m")),
                    func.min(_num("nearest_distance_m")),
                ).label("nearest_m"),
                func.avg(_num("mean_dwell_s")).label("avg_dwell_s"),
                func.avg(_num("people_nearby")).label("avg_people_nearby"),
                func.max(_int("people_nearby")).label("peak_people_nearby"),
                func.coalesce(func.sum(_int("person_count")), 0).label("total_reach"),
                func.coalesce(func.sum(_int("looked_count")), 0).label("total_looked"),
                # unique bodies that ENTERED the lidar field, summed across ticks
                # (the SDK counts each person once on entry) -> "people passed by".
                func.coalesce(func.sum(_int("lidar_passed")), 0).label("total_passed_lidar"),
            )
            .select_from(join)
            .where(EventRaw.event_type == "scene_observed")
        )
    ).one()

    breakdown = await _interaction_breakdown(session, display_id, start, end)
    total_interactions = sum(breakdown.values())
    phones_out = breakdown.get("phone_out", 0)

    nearest = scene_row.nearest_m
    dwell = scene_row.avg_dwell_s
    avg_nearby = scene_row.avg_people_nearby
    peak_nearby = scene_row.peak_people_nearby
    total_reach = int(scene_row.total_reach)
    total_looked = int(scene_row.total_looked)
    total_passed_lidar = int(scene_row.total_passed_lidar)

    return {
        "samples": int(scene_row.samples),
        # unique "people passed by" from the lidar's wide field of view; distinct
        # from total_reach (a per-frame camera sum). 0 until a lidar-equipped
        # robot streams — the camera metrics stand alone until then.
        "total_passed_lidar": total_passed_lidar,
        "avg_reach": round(float(scene_row.avg_reach), 1),
        "peak_reach": int(scene_row.peak_reach),
        "avg_attended": round(float(scene_row.avg_attended), 1),
        # avg_dwell_s = 0.0 is the "no data" SENTINEL (same contract as
        # audience.py) — consumers guard on the value, not on `samples`.
        "avg_dwell_s": round(float(dwell), 1) if dwell is not None else 0.0,
        "nearest_m": round(float(nearest), 1) if nearest is not None else None,
        "avg_people_nearby": round(float(avg_nearby), 1) if avg_nearby is not None else None,
        "peak_people_nearby": int(peak_nearby) if peak_nearby is not None else None,
        "total_reach": total_reach,
        "total_looked": total_looked,
        "look_rate": round(total_looked / total_reach, 3) if total_reach > 0 else None,
        "total_phones_out": int(phones_out),
        "total_interactions": int(total_interactions),
        "interaction_breakdown": breakdown,
    }


async def _interaction_breakdown(
    session: AsyncSession, display_id: uuid.UUID, start: datetime, end: datetime
) -> dict[str, int]:
    """``{kind: count}`` over attributed ``interaction_observed`` events."""
    join = EventRaw.__table__.join(
        DisplayAssignment.__table__, _attributed_to(display_id, start, end)
    )
    kind = func.coalesce(EventRaw.payload["kind"].astext, "other")
    rows = (
        await session.execute(
            select(kind.label("kind"), func.count().label("n"))
            .select_from(join)
            .where(EventRaw.event_type == "interaction_observed")
            .group_by(kind)
        )
    ).all()
    return {str(r.kind): int(r.n) for r in rows}


async def recent_display_events(
    session: AsyncSession,
    display_id: uuid.UUID,
    start: datetime,
    end: datetime,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Newest attributed events, mapped to the live-feed shape (real timestamps).

    This is the genuine replacement for ``HawkeyeFeed``'s ``Math.random`` stream:
    a ``scene_observed`` becomes a ``view`` line, an ``interaction_observed``
    becomes its ``kind``.
    """
    join = EventRaw.__table__.join(
        DisplayAssignment.__table__, _attributed_to(display_id, start, end)
    )
    rows = (
        await session.execute(
            select(EventRaw.event_type, EventRaw.payload, EventRaw.timestamp)
            .select_from(join)
            .where(EventRaw.event_type.in_(("scene_observed", "interaction_observed")))
            .order_by(EventRaw.timestamp.desc())
            .limit(limit)
        )
    ).all()

    out: list[dict[str, Any]] = []
    for event_type, payload, ts in rows:
        p = dict(payload or {})
        if event_type == "interaction_observed":
            out.append({"kind": str(p.get("kind", "other")), "ts": ts})
        else:
            out.append(
                {
                    "kind": "view",
                    "person_count": int(p.get("person_count", 0) or 0),
                    "attended_count": int(p.get("attended_count", 0) or 0),
                    "ts": ts,
                }
            )
    return out


async def latest_radar(
    session: AsyncSession, display_id: uuid.UUID, start: datetime, end: datetime
) -> dict[str, Any] | None:
    """The most recent attributed scene's lidar blips, for the live 360° radar.

    Returns the newest ``scene_observed`` in the window that actually carries
    ``lidar_people`` (a lidar-equipped robot) as ``{blips, people_nearby,
    nearest_m, ts}``; ``blips`` is ``[[range_m, bearing_deg], ...]`` (bearing
    0=front, +=right). ``None`` when no lidar frame exists — the UI then keeps
    the radar idle rather than animating fake points.
    """
    join = EventRaw.__table__.join(
        DisplayAssignment.__table__, _attributed_to(display_id, start, end)
    )
    row = (
        await session.execute(
            select(EventRaw.payload, EventRaw.timestamp)
            .select_from(join)
            .where(
                EventRaw.event_type == "scene_observed",
                EventRaw.payload.has_key("lidar_people"),  # noqa: W601 - JSONB ?
            )
            .order_by(EventRaw.timestamp.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    payload, ts = row
    p = dict(payload or {})
    blips = p.get("lidar_people") or []
    # keep only well-formed [range, bearing] pairs; cap for a sane radar
    clean = [
        [float(b[0]), float(b[1])]
        for b in blips
        if isinstance(b, (list, tuple)) and len(b) >= 2
    ][:32]
    return {
        "blips": clean,
        "people_nearby": p.get("people_nearby"),
        "nearest_m": p.get("nearest_distance_m"),
        "ts": ts,
    }


async def active_assignments(
    session: AsyncSession, display_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Robots currently (open interval) showing this display, with since-when."""
    rows = (
        await session.execute(
            select(DisplayAssignment.robot_id, DisplayAssignment.effective_from)
            .where(
                DisplayAssignment.display_id == display_id,
                DisplayAssignment.effective_to.is_(None),
            )
            .order_by(DisplayAssignment.effective_from.desc())
        )
    ).all()
    return [{"robot_id": str(r.robot_id), "since": r.effective_from} for r in rows]
