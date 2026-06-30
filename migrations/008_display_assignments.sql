-- 008_display_assignments
-- Binds an OEM custom display to the robots that are showing it, over time, so
-- the cloud can attribute the perception metrics it already receives (the
-- scene_observed / interaction_observed events on events_raw, each already
-- stamped with robot_id + timestamp) to the right custom display.
--
-- This is the whole "which campaign is playing, and where" mechanism: no robot
-- SDK change. A row says "robot R was showing display D from effective_from
-- until effective_to (NULL = still showing)". The live/per-display views join
-- events_raw to this history by robot_id and event timestamp ∈ [from, to).
--
-- Robot-level granularity (a fleet-wide assignment is just one row per robot).
-- Insight-only: no budget, no cost, no Stripe — these displays never touch the
-- spend processor or the impressions table.
--
-- IF NOT EXISTS so re-running against an environment that already has it is a
-- no-op (matches the rest of this folder's forward-only style). The ORM
-- (models.py) mirrors this table.

CREATE TABLE IF NOT EXISTS display_assignments (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_id     UUID NOT NULL REFERENCES custom_displays(id) ON DELETE CASCADE,
    robot_id       UUID NOT NULL REFERENCES robots(id) ON DELETE CASCADE,
    -- Half-open interval [effective_from, effective_to). effective_to NULL means
    -- the assignment is still active. An event at time T belongs to this display
    -- when effective_from <= T AND (effective_to IS NULL OR T < effective_to).
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT display_assignments_interval_check
        CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- Attribution join: "what was robot R showing at time T" — robot_id + the time
-- window. effective_from DESC so the current (open) assignment is found first.
CREATE INDEX IF NOT EXISTS ix_display_assignments_robot
    ON display_assignments(robot_id, effective_from DESC);

-- Per-display rollup: "all robots/intervals for display D".
CREATE INDEX IF NOT EXISTS ix_display_assignments_display
    ON display_assignments(display_id, effective_from DESC);

-- At most one OPEN (currently-playing) assignment per robot, so a robot is never
-- ambiguously showing two displays at once. Closing the old one before opening a
-- new one is the reassignment protocol (see routes/oem.py assign handler).
CREATE UNIQUE INDEX IF NOT EXISTS ux_display_assignments_open_robot
    ON display_assignments(robot_id)
    WHERE effective_to IS NULL;
