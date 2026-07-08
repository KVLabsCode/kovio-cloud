"""Pydantic 2 request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --- Health -------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    time: datetime
    db_ok: bool


# --- SDK: campaigns -----------------------------------------------------------
class CampaignOut(BaseModel):
    """Matches the SDK's Campaign.from_dict(): note creative_path = creative_url."""

    campaign_id: str
    name: str
    advertiser: str
    creative_path: str
    targeting: list[dict[str, Any]]
    priority: int
    encounter_cap_seconds: int
    enabled: bool


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignOut]
    fetched_at: datetime
    ttl_seconds: int = 300


# --- SDK: events --------------------------------------------------------------
class EventIn(BaseModel):
    event_id: uuid.UUID
    timestamp: float = Field(..., description="Epoch seconds (on-robot wall clock).")
    event_type: str
    robot_id: str = Field(..., description="The robot's external_id, not the UUID.")
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventBatchResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: int


# --- SDK: heartbeat -----------------------------------------------------------
class HeartbeatIn(BaseModel):
    robot_id: str = Field(..., description="The robot's external_id.")
    status: str = "online"
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatResponse(BaseModel):
    ok: bool
    robot_id: uuid.UUID
    registered: bool


# --- Admin: create payloads ---------------------------------------------------
class OrgCreate(BaseModel):
    name: str
    slug: str
    kind: str
    balance_cents: int = 0


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    kind: str
    status: str
    balance_cents: int
    pending_payout_cents: int
    lifetime_payout_cents: int
    created_at: datetime

    model_config = {"from_attributes": True}


class FleetCreate(BaseModel):
    org_id: uuid.UUID
    name: str
    region: str | None = None
    blocked_categories: list[str] = Field(default_factory=list)
    blocked_advertisers: list[uuid.UUID] = Field(default_factory=list)
    revenue_share_pct: float = 60.00


class FleetOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    region: str | None
    revenue_share_pct: float
    created_at: datetime

    model_config = {"from_attributes": True}


class RobotCreate(BaseModel):
    fleet_id: uuid.UUID
    external_id: str
    status: str = "provisioning"
    meta: dict[str, Any] = Field(default_factory=dict)


class RobotOut(BaseModel):
    id: uuid.UUID
    fleet_id: uuid.UUID
    external_id: str
    status: str
    last_heartbeat: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    org_id: uuid.UUID
    fleet_id: uuid.UUID | None = None
    name: str
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    fleet_id: uuid.UUID | None
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    # Only populated on creation — the plaintext key is never stored or shown again.
    api_key: str | None = None

    model_config = {"from_attributes": True}


class CampaignCreate(BaseModel):
    org_id: uuid.UUID
    campaign_id: str
    name: str
    advertiser: str = ""
    creative_url: str
    targeting: list[dict[str, Any]] = Field(default_factory=list)
    priority: int = 10
    encounter_cap_seconds: int = 300
    category: str | None = None
    budget_total_cents: int = 0
    cost_per_impression_cents: float = 1.0
    cost_per_attended_cents: float = 5.0
    cost_per_engagement_cents: float = 50.0


class CampaignAdminOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    campaign_id: str
    name: str
    advertiser: str
    status: str
    enabled: bool
    budget_total_cents: int
    budget_spent_cents: int
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Admin: stats -------------------------------------------------------------
class StatsSummary(BaseModel):
    organizations: int
    fleets: int
    robots: int
    campaigns: int
    events_24h: int
    impressions_24h: int
    total_spent_24h_cents: int
    total_pending_payouts_cents: int


# --- Spend processor result ---------------------------------------------------
class SpendRunResult(BaseModel):
    events_processed: int
    impressions_created: int
    campaigns_paused: list[str]
    advertisers_drained: list[str]


# --- Sessions (admin live-view windows, migration 009) --------------------------
class SessionRobotOut(BaseModel):
    id: uuid.UUID
    external_id: str
    status: str
    last_heartbeat: datetime | None
    online: bool

    model_config = {"from_attributes": True}


class SessionRobotsResponse(BaseModel):
    robots: list[SessionRobotOut]
    online_threshold_seconds: int


class SessionStartIn(BaseModel):
    robot_id: uuid.UUID
    display_id: uuid.UUID | None = None


class SessionStopIn(BaseModel):
    robot_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    robot_id: uuid.UUID
    fleet_id: uuid.UUID
    org_id: uuid.UUID
    display_id: uuid.UUID | None
    status: str
    started_at: datetime
    ended_at: datetime | None

    model_config = {"from_attributes": True}


class SessionCurrentOut(BaseModel):
    active: bool
    session_id: uuid.UUID | None = None
    started_at: datetime | None = None
    frame_interval_seconds: int = 5


class SessionSummaryOut(BaseModel):
    session_id: uuid.UUID
    status: str
    started_at: datetime
    ended_at: datetime | None
    impressions: int
    person_count: int
    attended_count: int
    latest_campaign: str | None
    last_frame_age_seconds: float | None
