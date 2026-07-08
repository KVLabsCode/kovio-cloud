-- 010_session_campaign_perception
-- V2 audience perception + reporting (spec 2026-07-09). Three additive pieces:
--
--  1. sessions gains the campaign binding asserted at Start: a single-creative
--     session binds ONE campaign (campaign_id); a looping/multi-creative
--     session is BLENDED (is_blended, campaign_id stays NULL) and its metrics
--     roll up under the display, never a campaign.
--  2. audience_samples (already present, previously unwritten) gains session
--     linkage and the fields the three on-device metrics produce. One row =
--     one MOMENT: metric_kind 'passerby' | 'dwell' | 'close_approach', keyed
--     by a session-scoped LiDAR track_id (dedup: unique reach = DISTINCT
--     track_id). event_id (already UNIQUE) carries the robot's moment id, so
--     retried uploads are idempotent.
--  3. demo_creatives — a small reusable creative library (org-scoped; org_id
--     NULL = the global Kovio demo set) loadable into any custom display.
--
-- Additive only: no existing column is altered, dropped or retyped; all new
-- columns are nullable (or defaulted). No billing surface: nothing here is
-- read by spend_processor/settlement. The ORM (models.py) mirrors all three.

-- 1. session <-> campaign binding + blended flag
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS campaign_id UUID REFERENCES campaigns(id),
    ADD COLUMN IF NOT EXISTS is_blended  BOOLEAN NOT NULL DEFAULT FALSE;

-- 2. audience_samples: session linkage + moment fields
ALTER TABLE audience_samples
    ADD COLUMN IF NOT EXISTS session_id       UUID REFERENCES sessions(id),
    ADD COLUMN IF NOT EXISTS track_id         BIGINT,
    ADD COLUMN IF NOT EXISTS metric_kind      TEXT,
    ADD COLUMN IF NOT EXISTS dwell_tier       TEXT,
    ADD COLUMN IF NOT EXISTS camera_confirmed BOOLEAN,
    ADD COLUMN IF NOT EXISTS lidar_confirmed  BOOLEAN;

CREATE INDEX IF NOT EXISTS ix_audience_samples_session
    ON audience_samples(session_id);
-- (campaign_id, "timestamp" DESC) already exists: ix_audience_samples_campaign_ts.

-- 3. demo creative library
CREATE TABLE IF NOT EXISTS demo_creatives (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- NULL org = the global Kovio demo set every org can load.
    org_id          UUID REFERENCES organizations(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    media_url       TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    default_seconds INTEGER NOT NULL DEFAULT 8,
    is_demo         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT demo_creatives_media_type_check
        CHECK (media_type IN ('image', 'video'))
);

CREATE INDEX IF NOT EXISTS ix_demo_creatives_org ON demo_creatives(org_id);
