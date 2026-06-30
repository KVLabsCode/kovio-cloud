-- 007_impressions_interaction_metrics
-- Enriched on-device perception metrics, correlated onto each impression the
-- same way person/attended/min_distance already are: the spend processor reads
-- the concurrent scene_observed sample (now carrying dwell, gaze and lidar crowd
-- fields) plus a count of interaction_observed events in the same window.
--
-- These are INSIGHT-ONLY for v1: they power the dashboards' audience panel and
-- the engagement funnel, and are available for campaign targeting, but no money
-- moves on them (cost_cents is unchanged). Pricing can be wired later without a
-- schema change. Every column is nullable or defaulted so back-filled and
-- pre-007 impressions remain valid; NULL surfaces as "—" in the UI.
ALTER TABLE impressions
    -- attention & dwell (depth camera + tracker)
    ADD COLUMN IF NOT EXISTS looked_count          INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS mean_dwell_s          NUMERIC(6, 2),
    -- crowd & proximity (lidar, wide field of view)
    ADD COLUMN IF NOT EXISTS people_nearby         INTEGER,
    ADD COLUMN IF NOT EXISTS crowd_density         NUMERIC(8, 4),
    ADD COLUMN IF NOT EXISTS nearest_distance_m    NUMERIC(6, 2),
    -- discrete interactions (pose + object detection)
    ADD COLUMN IF NOT EXISTS phones_out            INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS interactions          INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS interaction_breakdown JSONB   NOT NULL DEFAULT '{}'::jsonb;
