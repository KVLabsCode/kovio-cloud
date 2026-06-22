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
                func.min(Impression.min_distance_m).label("nearest_m"),
            ).where(*conditions)
        )
    ).one()

    nearest = row.nearest_m
    return {
        "samples": int(row.samples),
        "avg_reach": round(float(row.avg_reach), 1),
        "peak_reach": int(row.peak_reach),
        "avg_attended": round(float(row.avg_attended), 1),
        # No dwell column yet: avg_dwell_s=0.0 is a SENTINEL meaning "no data",
        # not a measured zero. Consumers MUST guard on the value (avg_dwell_s > 0
        # ? ... : "—"), NOT on `samples`, which can be > 0 while dwell is absent.
        "avg_dwell_s": 0.0,
        # Closest recorded approach (metres) across the window's impressions;
        # None when no impression carried LiDAR proximity data -> UI shows "—".
        "nearest_m": round(float(nearest), 1) if nearest is not None else None,
    }
