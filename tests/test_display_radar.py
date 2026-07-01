"""The live-panel 360° radar path: seeding must land a LiDAR-bearing
``scene_observed`` inside the 5-minute live window, attributed to the demo
display, so ``latest_radar`` returns real blips instead of ``None`` (which is
what leaves ``HawkeyeRadar`` stuck on "awaiting lidar…").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select


async def test_latest_radar_returns_blips_after_seeding(clean_db):
    from kovio_cloud.cli import _bootstrap, _seed_events
    from kovio_cloud.db import session_scope
    from kovio_cloud.display_insights import latest_radar
    from kovio_cloud.models import CustomDisplay

    await _bootstrap()
    result = await _seed_events(per_campaign=2)
    # The seed emits a burst of lidar frames packed into the last ~3 minutes.
    assert result["live_radar_burst"] > 0

    # The same 5-minute window the OEM /displays/{id}/live route uses.
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)

    async with session_scope() as session:
        display = (
            await session.execute(
                select(CustomDisplay).where(CustomDisplay.code == result["display_code"])
            )
        ).scalar_one()
        radar = await latest_radar(session, display.id, start, end)

    assert radar is not None, "the 5-min live window must catch a seeded lidar frame"
    blips = radar["blips"]
    assert blips, "radar must carry non-empty [range_m, bearing_deg] blips"
    # Blips are nearest-first and within the documented physical ranges.
    ranges = [rng for rng, _ in blips]
    assert ranges == sorted(ranges), "blips must be ordered nearest-first"
    for rng, bearing in blips:
        assert 0.0 < rng <= 4.0
        assert -90.0 <= bearing <= 90.0
    # The derived scalars the radar surfaces are populated too.
    assert radar["people_nearby"] is not None
    assert radar["nearest_m"] == blips[0][0]


async def test_latest_radar_none_without_assignment(clean_db):
    """Sanity check the guard direction: a fresh display with no assigned robot
    (hence no attributed events) yields ``None`` — the idle-radar case.
    """
    from kovio_cloud.db import session_scope
    from kovio_cloud.display_insights import latest_radar
    from kovio_cloud.models import CustomDisplay, Organization

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)

    async with session_scope() as session:
        org = Organization(name="Lonely OEM", slug="lonely-oem", kind="oem")
        session.add(org)
        await session.flush()
        display = CustomDisplay(org_id=org.id, code="lonely-display", name="Lonely")
        session.add(display)
        await session.flush()
        radar = await latest_radar(session, display.id, start, end)

    assert radar is None
