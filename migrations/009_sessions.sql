-- 009_sessions
-- Admin "session" = a recorded start/stop window during which one robot is
-- live-streamed (in-RAM JPEG relay, never persisted) and its impressions are
-- attributed to whatever it is playing. A session row is only the binding:
-- which robot, which custom display (if any), and the [started_at, ended_at)
-- window. Impressions/events are NOT written here — summaries are read-only
-- timestamp-range queries over the existing events_raw/impressions tables, so
-- the spend processor and settlement math are untouched.
--
-- Additive-only per the /admin/sessions build scope: new table, no changes to
-- any existing table. IF NOT EXISTS matches this folder's forward-only style.
-- The ORM (models.py) mirrors this table.

CREATE TABLE IF NOT EXISTS sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    robot_id   UUID NOT NULL REFERENCES robots(id) ON DELETE CASCADE,
    fleet_id   UUID NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    org_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- The custom display the admin set up for this run; SET NULL so deleting a
    -- display keeps the session history.
    display_id UUID REFERENCES custom_displays(id) ON DELETE SET NULL,
    status     VARCHAR(20) NOT NULL DEFAULT 'recording',
    -- Half-open window [started_at, ended_at). ended_at NULL = still recording.
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sessions_status_check CHECK (status IN ('recording', 'stopped')),
    CONSTRAINT sessions_interval_check
        CHECK (ended_at IS NULL OR ended_at > started_at)
);

-- "What is robot R's current/latest session" — the robot polls this every 5s.
CREATE INDEX IF NOT EXISTS ix_sessions_robot_started
    ON sessions(robot_id, started_at DESC);

-- At most one OPEN session per robot (same invariant style as
-- ux_display_assignments_open_robot): start closes the old one first.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_open_robot
    ON sessions(robot_id)
    WHERE ended_at IS NULL;
