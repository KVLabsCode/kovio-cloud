"""Typer CLI: bootstrap (seed), serve (run API), process-spend (one pass)."""

from __future__ import annotations

import asyncio
import json

import typer

from .auth import generate_api_key
from .config import get_settings
from .db import get_logger, session_scope
from .models import ApiKey, Campaign, Fleet, Organization, Robot, Transaction

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
