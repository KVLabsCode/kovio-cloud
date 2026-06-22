"""The spend processor: turns raw ``ad_played`` events into costed impressions,
moves money atomically, and pauses campaigns that hit their budget or drain
their advertiser's balance.

Concurrency: each candidate event is re-selected ``FOR UPDATE SKIP LOCKED`` in
its own transaction, so multiple processor instances never double-process an
event. Each event is fully costed in ONE transaction — a failure on event #50
does not roll back events #1-49.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_logger, get_sessionmaker
from .models import Campaign, EventRaw, Fleet, Impression, Organization, Transaction

log = get_logger("kovio_cloud.spend")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cents(value: Decimal) -> int:
    """Round a Decimal cent amount to the nearest whole integer cent."""

    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# How far back to look for the LiDAR scene that was current when an ad played.
_SCENE_WINDOW = timedelta(seconds=300)


async def _scene_for_event(session: AsyncSession, event: EventRaw) -> dict:
    """Resolve the LiDAR scene concurrent with an ``ad_played`` event.

    Robots emit ``scene_observed`` (``person_count`` / ``attended_count`` /
    ``mean_distance_m``) continuously and ``ad_played`` when a creative shows;
    the scene sampled at/just-before the play is what that impression actually
    reached. The ``ad_played`` payload itself carries no audience counts, so we
    correlate by robot + timestamp. Returns neutral values (0 / None) when no
    scene is available — robot unknown, or no sample within ``_SCENE_WINDOW``.
    """

    empty = {"person": 0, "attended": 0, "mean_distance_m": None}
    if event.robot_id is None:
        return empty
    payload = (
        await session.execute(
            select(EventRaw.payload)
            .where(
                EventRaw.robot_id == event.robot_id,
                EventRaw.event_type == "scene_observed",
                EventRaw.timestamp <= event.timestamp,
                EventRaw.timestamp >= event.timestamp - _SCENE_WINDOW,
            )
            .order_by(EventRaw.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not payload:
        return empty
    p = dict(payload)
    return {
        "person": int(p.get("person_count", 0) or 0),
        "attended": int(p.get("attended_count", 0) or 0),
        "mean_distance_m": p.get("mean_distance_m"),
    }


async def process_pending_events(session: AsyncSession, limit: int = 1000) -> dict:
    """Cost up to ``limit`` unprocessed ad_played events. Returns a summary dict."""

    events_processed = 0
    impressions_created = 0
    campaigns_paused: list[str] = []
    advertisers_drained: list[str] = []

    # Snapshot the candidate ids (read-only), in its own short transaction so the
    # per-event ``session.begin()`` blocks below start from a clean slate. Each id
    # is then re-locked individually so we get per-event commits + SKIP LOCKED.
    async with session.begin():
        candidate_ids = (
            await session.execute(
                select(EventRaw.event_id)
                .where(EventRaw.event_type == "ad_played", EventRaw.processed_at.is_(None))
                .order_by(EventRaw.received_at.asc())
                .limit(limit)
            )
        ).scalars().all()

    for event_id in candidate_ids:
        async with session.begin():
            # Re-select with a row lock; skip if another worker grabbed it or it
            # was processed since the snapshot.
            event = (
                await session.execute(
                    select(EventRaw)
                    .where(EventRaw.event_id == event_id, EventRaw.processed_at.is_(None))
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if event is None:
                continue

            payload = dict(event.payload or {})
            campaign_code = payload.get("campaign_id")

            # --- No campaign reference: mark processed, don't cost --------------
            if not campaign_code:
                event.processed_at = _now()
                events_processed += 1
                log.warning("event_no_campaign_id", event_id=str(event_id))
                continue

            campaign = (
                await session.execute(
                    select(Campaign).where(Campaign.campaign_id == campaign_code)
                )
            ).scalar_one_or_none()

            # --- Unknown or disabled campaign: mark processed, don't cost -------
            if campaign is None or not campaign.enabled:
                event.processed_at = _now()
                events_processed += 1
                log.warning(
                    "event_campaign_missing_or_disabled",
                    event_id=str(event_id),
                    campaign_id=campaign_code,
                )
                continue

            fleet = (
                await session.execute(select(Fleet).where(Fleet.id == event.fleet_id))
            ).scalar_one()

            advertiser = (
                await session.execute(
                    select(Organization).where(Organization.id == campaign.org_id)
                )
            ).scalar_one()
            oem = (
                await session.execute(
                    select(Organization).where(Organization.id == fleet.org_id)
                )
            ).scalar_one()

            # --- Audience: correlate the concurrent LiDAR scene ----------------
            # ad_played carries no audience counts; source them (and proximity)
            # from the scene the robot observed when the creative showed. A
            # payload value, if ever present, wins over the correlated scene.
            scene = await _scene_for_event(session, event)
            attended = int(payload.get("attended_count", scene["attended"]) or 0)
            person = int(payload.get("person_count", scene["person"]) or 0)
            min_distance_m = payload.get("mean_distance_m", scene["mean_distance_m"])

            # --- Gross cost: the campaign's real price for this impression. Used
            # for budget accounting for EVERY campaign (incl. promos), so the
            # budget still exhausts and the campaign pauses.
            cost = Decimal(campaign.cost_per_impression_cents)
            if attended > 0:
                cost += Decimal(campaign.cost_per_attended_cents) * attended
            gross_cost_cents = _cents(cost)

            # --- Money movement. A free-tier promo records the impression (so
            # reach/attention/proximity data flows) and accrues its gross cost
            # against budget_spent_cents — so budget_total_cents is a real, finite
            # free quota that still pauses the campaign — but debits nothing and
            # writes no ledger. Paid campaigns charge the gross cost as before.
            if campaign.is_promo:
                charge_cents = 0
                revenue_to_oem_cents = 0
                kovio_share_cents = 0
            else:
                charge_cents = gross_cost_cents
                share_pct = Decimal(fleet.revenue_share_pct)
                revenue_to_oem_cents = _cents(
                    Decimal(charge_cents) * share_pct / Decimal(100)
                )
                kovio_share_cents = charge_cents - revenue_to_oem_cents

            # --- Insufficient balance: decline, pause, don't create impression -
            if charge_cents > 0 and advertiser.balance_cents - charge_cents < 0:
                payload["declined"] = True
                payload["reason"] = "insufficient_balance"
                event.payload = payload
                event.processed_at = _now()
                if campaign.status != "paused":
                    campaign.status = "paused"
                    campaigns_paused.append(campaign.campaign_id)
                if advertiser.slug not in advertisers_drained:
                    advertisers_drained.append(advertiser.slug)
                events_processed += 1
                log.warning(
                    "advertiser_insufficient_balance",
                    event_id=str(event_id),
                    campaign_id=campaign.campaign_id,
                    advertiser=advertiser.slug,
                    balance_cents=advertiser.balance_cents,
                    cost_cents=charge_cents,
                )
                continue

            # --- Happy path: create impression + ledger + move money -----------
            impression = Impression(
                event_id=event.event_id,
                campaign_id=campaign.id,
                advertiser_org_id=advertiser.id,
                oem_org_id=oem.id,
                fleet_id=fleet.id,
                robot_id=event.robot_id,
                person_count=person,
                attended_count=attended,
                min_distance_m=min_distance_m,
                cost_cents=charge_cents,
                revenue_to_oem_cents=revenue_to_oem_cents,
                kovio_share_cents=kovio_share_cents,
                timestamp=event.timestamp,
            )
            session.add(impression)
            await session.flush()  # assign impression.id for ledger references

            # Promo impressions move no money, so they create no ledger entries.
            if charge_cents > 0:
                session.add_all(
                    [
                        Transaction(
                            org_id=advertiser.id,
                            kind="impression_charge",
                            amount_cents=-charge_cents,
                            reference_type="impression",
                            reference_id=str(impression.id),
                        ),
                        Transaction(
                            org_id=oem.id,
                            kind="oem_accrual",
                            amount_cents=revenue_to_oem_cents,
                            reference_type="impression",
                            reference_id=str(impression.id),
                        ),
                    ]
                )

            # Budget accrues the GROSS cost for every campaign (promos included)
            # so budget_total_cents is a finite quota that pauses the campaign;
            # only real (non-promo) charges move money.
            campaign.budget_spent_cents += gross_cost_cents
            advertiser.balance_cents -= charge_cents
            oem.pending_payout_cents += revenue_to_oem_cents

            if (
                campaign.budget_total_cents > 0
                and campaign.budget_spent_cents >= campaign.budget_total_cents
                and campaign.status != "paused"
            ):
                campaign.status = "paused"
                campaigns_paused.append(campaign.campaign_id)
                log.info(
                    "campaign_budget_exhausted",
                    campaign_id=campaign.campaign_id,
                    budget_spent_cents=campaign.budget_spent_cents,
                    budget_total_cents=campaign.budget_total_cents,
                )

            event.processed_at = _now()
            events_processed += 1
            impressions_created += 1

    if events_processed:
        log.info(
            "spend_run_complete",
            events_processed=events_processed,
            impressions_created=impressions_created,
            campaigns_paused=campaigns_paused,
            advertisers_drained=advertisers_drained,
        )

    return {
        "events_processed": events_processed,
        "impressions_created": impressions_created,
        "campaigns_paused": campaigns_paused,
        "advertisers_drained": advertisers_drained,
    }


async def run_once() -> dict:
    """Open a fresh session, do one pass. Used by the CLI and the admin endpoint."""

    sm = get_sessionmaker()
    async with sm() as session:
        return await process_pending_events(session)


async def spend_processor_loop(stop_event: asyncio.Event) -> None:
    """Background task: cost pending events every N seconds until stopped."""

    settings = get_settings()
    interval = settings.spend_processor_interval_seconds
    log.info("spend_processor_started", interval_seconds=interval)

    while not stop_event.is_set():
        try:
            await run_once()
        except Exception:  # never let the loop die
            log.exception("spend_processor_iteration_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    log.info("spend_processor_stopped")
