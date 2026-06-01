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
from datetime import datetime, timezone
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

            # --- Cost computation ----------------------------------------------
            attended = int(payload.get("attended_count", 0) or 0)
            person = int(payload.get("person_count", 0) or 0)

            cost = Decimal(campaign.cost_per_impression_cents)
            if attended > 0:
                cost += Decimal(campaign.cost_per_attended_cents) * attended
            cost_cents = _cents(cost)

            share_pct = Decimal(fleet.revenue_share_pct)
            revenue_to_oem_cents = _cents(Decimal(cost_cents) * share_pct / Decimal(100))
            kovio_share_cents = cost_cents - revenue_to_oem_cents

            # --- Insufficient balance: decline, pause, don't create impression -
            if advertiser.balance_cents - cost_cents < 0:
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
                    cost_cents=cost_cents,
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
                cost_cents=cost_cents,
                revenue_to_oem_cents=revenue_to_oem_cents,
                kovio_share_cents=kovio_share_cents,
                timestamp=event.timestamp,
            )
            session.add(impression)
            await session.flush()  # assign impression.id for ledger references

            session.add_all(
                [
                    Transaction(
                        org_id=advertiser.id,
                        kind="impression_charge",
                        amount_cents=-cost_cents,
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

            campaign.budget_spent_cents += cost_cents
            advertiser.balance_cents -= cost_cents
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
