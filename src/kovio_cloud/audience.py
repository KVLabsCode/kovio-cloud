"""Privacy-safe LiDAR audience aggregation.

Robots derive ``person_count`` (people in frame) and ``attended_count`` (people
who actually faced the screen) locally; only those integers ever reach the cloud,
landing on each ``impressions`` row. There is no image data anywhere.

The web dashboards render an "audience" panel (reach / attention / dwell /
proximity) from a summary of those two columns over a time window. Dwell and
proximity have no columns on ``impressions`` yet, so they report neutral values
(``avg_dwell_s = 0``, ``nearest_m = None``); the frontend shows "—" when
``samples`` is 0 or those values are absent.
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
            ).where(*conditions)
        )
    ).one()

    return {
        "samples": int(row.samples),
        "avg_reach": round(float(row.avg_reach), 1),
        "peak_reach": int(row.peak_reach),
        "avg_attended": round(float(row.avg_attended), 1),
        # No dwell/proximity columns on impressions yet. These are SENTINELS,
        # not measured zeros: avg_dwell_s=0.0 / nearest_m=None mean "no data".
        # Consumers MUST guard on the value (e.g. avg_dwell_s > 0 ? ... : "—"),
        # NOT on `samples`, which can be > 0 while dwell is still a placeholder.
        "avg_dwell_s": 0.0,
        "nearest_m": None,
    }
