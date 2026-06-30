"""Privacy-safe LiDAR audience aggregation.

Robots derive ``person_count`` (people in frame) and ``attended_count`` (people
who actually faced the screen) locally; only those integers ever reach the cloud,
landing on each ``impressions`` row. There is no image data anywhere.

The web dashboards render an "audience" panel (reach / attention / dwell /
proximity) from a summary of those columns over a time window. Proximity comes
from ``impressions.min_distance_m`` (the LiDAR ``mean_distance_m`` sampled when
each ad played, correlated in by the spend processor). Dwell has no column yet,
so it reports a neutral sentinel (``avg_dwell_s = 0``); the frontend shows "—"
when ``samples`` is 0 or a value is absent (``nearest_m`` is None when no
impression in the window carried proximity data).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Impression


async def audience_summary(session: AsyncSession, *conditions: Any) -> dict[str, Any]:
    """Aggregate person/attended counts over impressions matching ``conditions``.

    Returns an AudienceSummary-shaped dict (see kovio-web ``lib/types.ts``).
    """
    row = (
        await session.execute(
            select(
                func.count().label("samples"),
                func.coalesce(func.avg(Impression.person_count), 0).label("avg_reach"),
                func.coalesce(func.max(Impression.person_count), 0).label("peak_reach"),
                func.coalesce(func.avg(Impression.attended_count), 0).label("avg_attended"),
                # nearest approach: best (smallest) of depth-cam mean & lidar nearest
                func.least(
                    func.min(Impression.min_distance_m),
                    func.min(Impression.nearest_distance_m),
                ).label("nearest_m"),
                # dwell — avg ignores NULLs, so this is the real mean over rows
                # that carried dwell; None when none did (rendered as the sentinel).
                func.avg(Impression.mean_dwell_s).label("avg_dwell_s"),
                # crowd (lidar)
                func.avg(Impression.people_nearby).label("avg_people_nearby"),
                func.max(Impression.people_nearby).label("peak_people_nearby"),
                # funnel totals
                func.coalesce(func.sum(Impression.person_count), 0).label("total_reach"),
                func.coalesce(func.sum(Impression.looked_count), 0).label("total_looked"),
                func.coalesce(func.sum(Impression.phones_out), 0).label("total_phones_out"),
                func.coalesce(func.sum(Impression.interactions), 0).label("total_interactions"),
            ).where(*conditions)
        )
    ).one()

    nearest = row.nearest_m
    dwell = row.avg_dwell_s
    avg_nearby = row.avg_people_nearby
    peak_nearby = row.peak_people_nearby
    total_reach = int(row.total_reach)
    total_looked = int(row.total_looked)

    return {
        "samples": int(row.samples),
        "avg_reach": round(float(row.avg_reach), 1),
        "peak_reach": int(row.peak_reach),
        "avg_attended": round(float(row.avg_attended), 1),
        # Real dwell now (migration 007). avg_dwell_s=0.0 stays a SENTINEL for
        # "no data" — consumers MUST guard on the value (> 0 ? ... : "—"), NOT on
        # `samples`, which can be > 0 while dwell is absent on basic adapters.
        "avg_dwell_s": round(float(dwell), 1) if dwell is not None else 0.0,
        # Closest recorded approach (metres); None when no proximity data -> "—".
        "nearest_m": round(float(nearest), 1) if nearest is not None else None,
        # --- crowd (lidar wide FOV) ---
        "avg_people_nearby": round(float(avg_nearby), 1) if avg_nearby is not None else None,
        "peak_people_nearby": int(peak_nearby) if peak_nearby is not None else None,
        # --- engagement funnel: reach -> looked -> phone-out -> interactions ---
        "total_reach": total_reach,
        "total_looked": total_looked,
        "look_rate": round(total_looked / total_reach, 3) if total_reach > 0 else None,
        "total_phones_out": int(row.total_phones_out),
        "total_interactions": int(row.total_interactions),
        "interaction_breakdown": await _interaction_breakdown(session, *conditions),
    }


async def _interaction_breakdown(session: AsyncSession, *conditions: Any) -> dict[str, int]:
    """Sum each impression's ``interaction_breakdown`` JSONB into {kind: count}.

    Only rows that recorded interactions are read (``interactions > 0``), which
    keeps the set small, then the per-kind dicts are merged in Python. Returns {}
    when no interactions occurred in the window.
    """
    rows = (
        await session.execute(
            select(Impression.interaction_breakdown).where(
                *conditions, Impression.interactions > 0
            )
        )
    ).scalars().all()
    out: dict[str, int] = {}
    for bd in rows:
        if not bd:
            continue
        for kind, count in dict(bd).items():
            out[kind] = out.get(kind, 0) + int(count)
    return out
