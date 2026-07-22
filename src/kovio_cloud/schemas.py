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
    # V2 (migration 010): the campaign the operator asserts is on screen.
    # Only accepted for a single-creative display; a looping display's session
    # is blended and must not bind a campaign.
    campaign_id: uuid.UUID | None = None


class SessionStopIn(BaseModel):
    robot_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    robot_id: uuid.UUID
    fleet_id: uuid.UUID
    org_id: uuid.UUID
    display_id: uuid.UUID | None
    campaign_id: uuid.UUID | None = None
    is_blended: bool = False
    status: str
    started_at: datetime
    ended_at: datetime | None

    model_config = {"from_attributes": True}


class SessionCurrentOut(BaseModel):
    active: bool
    session_id: uuid.UUID | None = None
    started_at: datetime | None = None
    frame_interval_seconds: int = 5
    # Dedup window the robot's tracker should honour (bound campaign's
    # encounter_cap_seconds, else the 300s default).
    encounter_cap_seconds: int | None = None
    # Dashboard-driven TTS: a pending utterance for this robot, if any. The
    # robot speaks it once and de-dupes on speak_nonce across the recurring
    # poll. All three are null when nothing is queued.
    speak_text: str | None = None
    speak_nonce: str | None = None
    speak_volume: int | None = None
    # Greeting-on-Go: when set, the robot fetches this (fleet-key auth) and plays
    # the returned WAV out its Bluetooth speaker instead of onboard TTS. Shares
    # speak_nonce for de-dup, and takes precedence over speak_text when present.
    speak_audio_url: str | None = None
    # Push-to-talk: when set, the dashboard has opened a listening window. The
    # robot captures mic audio once per new listen_nonce, transcribes locally,
    # and POSTs the text to /utterance. Null when no window is open.
    listen_nonce: str | None = None


class SessionSpeakIn(BaseModel):
    robot_id: uuid.UUID
    text: str = Field(min_length=1, max_length=500)
    volume: int | None = Field(default=None, ge=0, le=100)


class SessionSpeakOut(BaseModel):
    ok: bool
    nonce: str


# --- Push-to-talk conversation ---------------------------------------------
class SessionListenIn(BaseModel):
    robot_id: uuid.UUID


class SessionListenOut(BaseModel):
    ok: bool
    nonce: str


class SessionUtteranceIn(BaseModel):
    """The robot's locally-transcribed speech for a listening window. ``nonce``
    is the listen_nonce it acted on (for logging/correlation)."""

    text: str = Field(min_length=1, max_length=1000)
    nonce: str | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


# --- V2 audience moments (migration 010) -----------------------------------
class MomentIn(BaseModel):
    """One on-device audience moment. Extra keys are ignored so newer robots
    can ship richer payloads without breaking older servers."""

    moment_id: uuid.UUID
    kind: str
    track_id: int
    t: float
    closest_m: float | None = None
    min_m: float | None = None
    dwell_s: float | None = None
    duration_s: float | None = None
    tier: str | None = None
    camera_confirmed: bool | None = None
    lidar_confirmed: bool | None = None
    first_seen: float | None = None

    model_config = {"extra": "ignore"}


class SensorHealthIn(BaseModel):
    lidar_ok: bool = False
    lidar_hz: float = 0.0
    depth_ok: bool = False
    tracks: int = 0

    model_config = {"extra": "ignore"}


class MomentsIn(BaseModel):
    moments: list[MomentIn] = []
    sensor: SensorHealthIn | None = None


class MomentsAck(BaseModel):
    accepted: int
    duplicates: int


class SensorHealthOut(BaseModel):
    lidar_ok: bool
    lidar_hz: float
    depth_ok: bool
    age_seconds: float | None = None


class SessionMetricsOut(BaseModel):
    """Live tiles for the session panel — unique-track counts over
    audience_samples for the session window, plus sensor health so a dead
    sensor shows DEGRADED instead of a silent zero."""

    session_id: uuid.UUID
    status: str
    started_at: datetime
    ended_at: datetime | None
    is_blended: bool
    campaign_id: uuid.UUID | None
    reach_unique: int
    passersby_gross: int
    dwell_paused_plus: int
    dwell_engaged_plus: int
    dwell_deep: int
    close_approaches: int
    sensor: SensorHealthOut | None
    degraded: bool  # LiDAR not delivering -> reach/dwell can't be trusted


class SessionCampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    advertiser: str
    status: str
    enabled: bool

    model_config = {"from_attributes": True}


# --- Demo creative library + playlist editing (migration 010) ----------------
class DemoCreativeOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID | None
    label: str
    media_url: str
    media_type: str
    default_seconds: int
    is_demo: bool

    model_config = {"from_attributes": True}


class DisplayItemOut(BaseModel):
    id: uuid.UUID
    media_url: str
    media_type: str
    duration_seconds: int | None
    position: int

    model_config = {"from_attributes": True}


class DisplayItemsOut(BaseModel):
    display_id: uuid.UUID
    name: str
    default_image_seconds: int
    items: list[DisplayItemOut]


class DisplayItemCreateIn(BaseModel):
    media_url: str
    media_type: str  # 'image' | 'video'
    duration_seconds: int | None = None


class DisplayItemPatchIn(BaseModel):
    duration_seconds: int | None = None


class DisplayItemsReorderIn(BaseModel):
    item_ids: list[uuid.UUID]


class LoadPresetIn(BaseModel):
    creative_ids: list[uuid.UUID]


# --- Audience rollups (read-only; settlement untouched) ----------------------
class AudienceSessionRow(BaseModel):
    session_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    is_blended: bool
    campaign_id: uuid.UUID | None
    display_id: uuid.UUID | None
    reach_unique: int
    passersby_gross: int
    dwell_engaged_plus: int
    dwell_deep: int
    close_approaches: int


class AudienceRollupOut(BaseModel):
    """Aggregate over audience_samples for one campaign or one display."""

    scope: str                      # 'campaign' | 'display'
    scope_id: uuid.UUID
    label: str
    blended: bool                   # display rollups: blended across creatives
    creative_count: int | None = None
    from_ts: datetime | None
    to_ts: datetime | None
    reach_unique: int
    passersby_gross: int
    dwell_paused_plus: int
    dwell_engaged_plus: int
    dwell_deep: int
    close_approaches: int
    sessions: list[AudienceSessionRow]


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
