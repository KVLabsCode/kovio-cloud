"""Typer CLI: bootstrap (seed), serve (run API), process-spend (one pass)."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

import typer
from sqlalchemy import select

from .auth import generate_api_key
from .config import get_settings
from .db import get_logger, session_scope
from .models import (
    ApiKey,
    Campaign,
    CustomDisplay,
    DisplayAssignment,
    EventRaw,
    Fleet,
    Organization,
    Robot,
    Transaction,
)

app = typer.Typer(help="Kovio cloud control-plane CLI.", no_args_is_help=True)
log = get_logger("kovio_cloud.cli")


# =====================================================================
# bootstrap
# =====================================================================
async def _bootstrap() -> dict[str, str]:
    """Seed realistic test data, including budgets, so the spend processor has
    something to chew on. Idempotent on uniqueness: a second run raises on the
    slug / campaign_id unique constraints rather than duplicating data.
    """

    admin_full, admin_prefix, admin_hash = generate_api_key()
    sdk_full, sdk_prefix, sdk_hash = generate_api_key()

    async with session_scope() as session:
        # --- Kovio Labs admin org + admin key (admin + sdk scopes) ------------
        kovio_labs = Organization(name="Kovio Labs", slug="kovio-labs", kind="admin")
        session.add(kovio_labs)
        await session.flush()
        session.add(
            ApiKey(
                org_id=kovio_labs.id,
                name="bootstrap-admin-key",
                key_prefix=admin_prefix,
                key_hash=admin_hash,
                scopes=["admin", "sdk"],
            )
        )

        # --- Three advertiser orgs, each pre-funded with $100 -----------------
        advertisers: dict[str, Organization] = {}
        for name, slug in [
            ("Cafe Astra", "cafe-astra"),
            ("Trattoria Bot", "trattoria-bot"),
            ("Kovio", "kovio-brand"),
        ]:
            org = Organization(
                name=name, slug=slug, kind="advertiser", balance_cents=10000
            )
            session.add(org)
            await session.flush()
            advertisers[slug] = org
            session.add(
                Transaction(
                    org_id=org.id,
                    kind="advertiser_deposit",
                    amount_cents=10000,
                    reference_type="bootstrap",
                    reference_id="seed-deposit",
                    metadata_={"note": "bootstrap seed $100 deposit"},
                )
            )

        # --- One OEM org + fleet + robot --------------------------------------
        oem = Organization(
            name="Demo Fleet Inc.",
            slug="demo-fleet-inc",
            kind="oem",
            balance_cents=0,
            pending_payout_cents=0,
        )
        session.add(oem)
        await session.flush()

        fleet = Fleet(org_id=oem.id, name="Demo Pilot Fleet", region="SF Bay Area")
        session.add(fleet)
        await session.flush()

        session.add(Robot(fleet_id=fleet.id, external_id="tank-001", status="online"))

        # --- SDK key scoped to the demo fleet (sdk scope only) ----------------
        session.add(
            ApiKey(
                org_id=oem.id,
                fleet_id=fleet.id,
                name="demo-fleet-sdk-key",
                key_prefix=sdk_prefix,
                key_hash=sdk_hash,
                scopes=["sdk"],
            )
        )

        # --- Three campaigns with budgets -------------------------------------
        session.add_all(
            [
                Campaign(
                    org_id=advertisers["cafe-astra"].id,
                    campaign_id="cafe_morning",
                    name="Cafe Astra — Morning Brew",
                    advertiser="Cafe Astra",
                    creative_url="creatives/cafe_morning.html",
                    targeting=[
                        {"field": "hour_of_day", "op": "between", "value": [6, 11]},
                        {"field": "person_count", "op": ">=", "value": 1},
                    ],
                    priority=10,
                    encounter_cap_seconds=60,
                    category="food",
                    budget_total_cents=5000,
                    cost_per_impression_cents=10,
                ),
                Campaign(
                    org_id=advertisers["trattoria-bot"].id,
                    campaign_id="trattoria_evening",
                    name="Trattoria Bot — Evening Specials",
                    advertiser="Trattoria Bot",
                    creative_url="creatives/trattoria_evening.html",
                    targeting=[
                        {"field": "hour_of_day", "op": "between", "value": [17, 21]},
                        {"field": "person_count", "op": ">=", "value": 1},
                    ],
                    priority=10,
                    encounter_cap_seconds=60,
                    category="food",
                    budget_total_cents=5000,
                    cost_per_impression_cents=10,
                ),
                Campaign(
                    org_id=advertisers["kovio-brand"].id,
                    campaign_id="kovio_brand",
                    name="Kovio — Brand Awareness",
                    advertiser="Kovio",
                    creative_url="creatives/kovio_brand.html",
                    targeting=[{"field": "person_count", "op": ">=", "value": 1}],
                    priority=1,
                    encounter_cap_seconds=30,
                    category="brand",
                    budget_total_cents=10000,
                    cost_per_impression_cents=5,
                ),
            ]
        )

    return {"admin_key": admin_full, "sdk_key": sdk_full}


@app.command()
def bootstrap(
    with_test_user: bool = typer.Option(
        False,
        "--with-test-user",
        help="Print instructions for testing the advertiser web-app flow end to end.",
    ),
) -> None:
    """Seed the database with orgs, fleet, robot, API keys, and campaigns."""

    try:
        keys = asyncio.run(_bootstrap())
    except Exception as exc:  # unique violation on a second run, etc.
        typer.secho(f"Bootstrap failed: {exc}", fg=typer.colors.RED, err=True)
        typer.secho(
            "If this is a duplicate-key error, the database is already seeded.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    typer.secho("\n=== Bootstrap complete ===", fg=typer.colors.GREEN, bold=True)
    typer.secho(
        "SAVE THESE NOW — they are shown once and never again:",
        fg=typer.colors.YELLOW,
        bold=True,
    )
    typer.echo(f"  ADMIN_API_KEY={keys['admin_key']}")
    typer.echo(f"  SDK_API_KEY={keys['sdk_key']}")

    if with_test_user:
        # We deliberately do NOT insert a fake Supabase user row. Real Supabase
        # user IDs come from real signups; faking them creates orphaned rows.
        typer.secho(
            "\n=== TEST MODE: advertiser web-app flow ===", fg=typer.colors.CYAN, bold=True
        )
        typer.secho(
            "No test user was created (Supabase user IDs come from real Auth signups).",
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "To test the advertiser flow end to end:\n"
            "  1. Sign up at app.kovio.dev with a real email (magic link).\n"
            "  2. Call POST /advertiser/v1/onboarding with the resulting Supabase JWT and\n"
            "     { \"org_name\": \"Test Brand\", \"org_slug\": \"test-brand-<random>\" }\n"
            "     to link the new Supabase user to a fresh Kovio org.\n"
            "  3. Then /advertiser/v1/me, /deposit, /campaigns, /dashboard all work for that org."
        )


# =====================================================================
# seed-events  (dev/test: give the spend processor + dashboards real data)
# =====================================================================

# Lidar radius the SDK's LidarSource scans (adapters/lidar.py radius_m=4.0); the
# density is bodies per m^2 within that disc, matching CrowdReading.
_LIDAR_RADIUS_M = 4.0
_LIDAR_DISC_AREA_M2 = math.pi * _LIDAR_RADIUS_M * _LIDAR_RADIUS_M


def _scene_payload(person: int, attended: int, mean_distance_m: float, seed: int) -> dict:
    """A ``scene_observed`` payload carrying realistic LiDAR fields, keyed EXACTLY
    like ``SceneState.scalar_payload()`` in kovio-py (types.py) so the cloud's
    attributed-events readers (``display_summary`` / ``latest_radar``) see the
    same JSON a real robot streams.

    ``lidar_people`` is a nearest-first list of ``[range_m, bearing_deg]`` blips
    (range 0.8–4.0 m, bearing −90…+90, 0=front, +=right); the derived scalars
    (``nearest_distance_m`` / ``approach_bearing_deg``) come from the nearest one.
    """
    # people_nearby (wide LiDAR FOV) is >= the camera's in-frame person_count.
    k = max(1, min(person + 1, 6))
    blips: list[list[float]] = []
    for j in range(k):
        rng = round(min(0.8 + j * 0.6 + (seed % 4) * 0.25, _LIDAR_RADIUS_M), 2)
        bearing = round(-90 + ((seed * 41 + j * 57) % 181), 1)  # -90..+90
        blips.append([rng, bearing])
    blips.sort(key=lambda b: b[0])  # nearest first, as the SDK emits
    nearest_range, nearest_bearing = blips[0]
    return {
        # --- depth-camera (original v0 contract) ---
        "person_count": person,
        "attended_count": attended,
        "mean_distance_m": mean_distance_m,
        "looked_count": attended,
        "mean_dwell_s": round(1.4 + (seed % 5) * 0.5, 1),
        # --- lidar: wide-FOV crowd & proximity + per-body radar blips ---
        "people_nearby": k,
        "crowd_density": round(k / _LIDAR_DISC_AREA_M2, 4),
        "nearest_distance_m": nearest_range,
        "approach_bearing_deg": nearest_bearing,
        "lidar_people": blips,
        "lidar_passed": 1 + (seed % 3),
    }


async def _seed_events(per_campaign: int) -> dict:
    """Emit paired scene_observed + ad_played events for the demo fleet so the
    spend processor produces impressions carrying real reach/attention/proximity,
    plus a zero-balance PROMO campaign on a tiny budget to exercise the
    free-tier exhaustion path. Idempotent-ish: event_ids are random, so re-runs
    add more events rather than failing.
    """

    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        fleet = (
            await session.execute(select(Fleet).where(Fleet.name == "Demo Pilot Fleet"))
        ).scalar_one_or_none()
        robot = (
            await session.execute(select(Robot).where(Robot.external_id == "tank-001"))
        ).scalar_one_or_none()
        if fleet is None or robot is None:
            raise RuntimeError("demo fleet/robot not found — run `bootstrap` first")

        campaigns = list(
            (await session.execute(select(Campaign).where(Campaign.is_promo.is_(False)))).scalars()
        )

        def _emit(event_type: str, payload: dict, ts: datetime) -> None:
            session.add(
                EventRaw(
                    event_id=uuid.uuid4(),
                    robot_id=robot.id,
                    fleet_id=fleet.id,
                    robot_external_id=robot.external_id,
                    event_type=event_type,
                    payload=payload,
                    timestamp=ts,
                )
            )

        created = 0
        for ci, c in enumerate(campaigns):
            for i in range(per_campaign):
                # Spread over ~20 days; the modulo keeps some plays inside 24h.
                minutes_ago = (i * 17 + ci * 5) % (20 * 24 * 60)
                ts = now - timedelta(minutes=minutes_ago)
                person = 1 + ((i + ci) % 5)  # 1..5 people in frame
                attended = max(0, person - (i % 3))  # <= person, some 0
                dist = round(1.0 + ((i * 7 + ci * 3) % 40) / 10.0, 2)  # 1.0..4.9 m
                _emit(
                    "scene_observed",
                    _scene_payload(person, attended, dist, seed=i * 3 + ci),
                    ts,
                )
                _emit(
                    "ad_played",
                    {
                        "campaign_id": c.campaign_id,
                        "advertiser": c.advertiser,
                        "creative_path": c.creative_url,
                    },
                    ts + timedelta(seconds=2),
                )
                created += 2

        # --- Promo path: a zero-balance advertiser's first (free) campaign on a
        # tiny budget, with enough plays to blow past it. After process-spend it
        # should PAUSE (budget exhausted) while moving no money. ----------------
        suffix = uuid.uuid4().hex[:6]
        promo_adv = Organization(
            name=f"Promo Tester {suffix}",
            slug=f"promo-tester-{suffix}",
            kind="advertiser",
            balance_cents=0,
        )
        session.add(promo_adv)
        await session.flush()
        promo = Campaign(
            org_id=promo_adv.id,
            campaign_id=f"promo_free_{suffix}",
            name="Promo Tester — Free First Campaign",
            advertiser=promo_adv.name,
            creative_url="creatives/promo.html",
            category="brand",
            budget_total_cents=30,  # 3 impressions @ 10c notional -> exhausts fast
            cost_per_impression_cents=10,
            is_promo=True,
            status="active",
            enabled=True,
        )
        session.add(promo)
        for i in range(8):
            ts = now - timedelta(minutes=i * 3)
            _emit(
                "scene_observed",
                {"person_count": 3, "attended_count": 2, "mean_distance_m": 2.0},
                ts,
            )
            _emit(
                "ad_played",
                {"campaign_id": promo.campaign_id, "advertiser": promo.advertiser},
                ts + timedelta(seconds=2),
            )
            created += 2

        # --- Live radar burst: scene_observed events carrying LiDAR blips packed
        # into the last ~3 minutes, so latest_radar's 5-minute live window always
        # catches a fresh frame (the historical spread above is too old for it).
        burst = 0
        for i in range(12):
            ts = now - timedelta(seconds=i * 15)  # 0..165 s ago, all within ~3 min
            person = 1 + (i % 4)
            attended = max(0, person - (i % 2))
            dist = round(1.2 + (i % 5) * 0.3, 2)
            _emit("scene_observed", _scene_payload(person, attended, dist, seed=i + 1), ts)
            created += 1
            burst += 1

        # --- Bind the demo robot to a custom display so the attributed-events JOIN
        # (display_assignments) resolves — without it, latest_radar/display_summary
        # see none of these events and the live panel stays idle. Idempotent:
        # reuse an existing display/open assignment on re-run. ------------------
        display = (
            await session.execute(
                select(CustomDisplay).where(CustomDisplay.code == "demo-live")
            )
        ).scalar_one_or_none()
        if display is None:
            display = CustomDisplay(
                org_id=fleet.org_id,
                fleet_id=fleet.id,
                code="demo-live",
                name="Demo Live Panel",
                advertiser_name="Kovio Demo",
            )
            session.add(display)
            await session.flush()

        # At most one OPEN assignment per robot (migration 008 partial-unique
        # index), so only create one when the robot has none open yet.
        open_assignment = (
            await session.execute(
                select(DisplayAssignment).where(
                    DisplayAssignment.robot_id == robot.id,
                    DisplayAssignment.effective_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if open_assignment is None:
            session.add(
                DisplayAssignment(
                    display_id=display.id,
                    robot_id=robot.id,
                    # Well before the whole historical spread AND the burst, so
                    # every emitted event falls inside this open interval.
                    effective_from=now - timedelta(days=21),
                    effective_to=None,
                )
            )

    return {
        "events_created": created,
        "paid_campaigns": len(campaigns),
        "promo_campaign": promo.campaign_id,
        "promo_advertiser_slug": promo_adv.slug,
        "live_radar_burst": burst,
        "display_code": display.code,
        "display_id": str(display.id),
    }


@app.command(name="seed-events")
def seed_events(
    per_campaign: int = typer.Option(
        40, "--per-campaign", help="scene_observed+ad_played pairs per paid campaign."
    ),
) -> None:
    """Seed demo LiDAR/ad events (run after `bootstrap`, then `process-spend`)."""

    result = asyncio.run(_seed_events(per_campaign))
    typer.secho(json.dumps(result, indent=2), fg=typer.colors.GREEN)
    typer.secho("Now run `process-spend` to cost these into impressions.", fg=typer.colors.CYAN)
    typer.secho(
        f"The OEM live panel for display '{result['display_code']}' now has a "
        "LiDAR radar (scene_observed blips in the 5-min window).",
        fg=typer.colors.CYAN,
    )


# =====================================================================
# serve
# =====================================================================
@app.command()
def serve() -> None:
    """Run the FastAPI service with uvicorn."""

    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "kovio_cloud.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


# =====================================================================
# process-spend
# =====================================================================
@app.command(name="process-spend")
def process_spend() -> None:
    """Run one synchronous pass of the spend processor and print the result."""

    from .spend_processor import run_once

    result = asyncio.run(run_once())
    typer.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
