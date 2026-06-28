-- 006_custom_displays
-- OEM-owned "custom displays". A fleet operator uploads creative(s) for one of
-- their own sourced advertisers and points a robot screen at /display/<code>,
-- which loops the items full-screen. Standalone from paid campaigns: no budget,
-- no QR, no Stripe. The ORM (models.py) mirrors these two tables.
--
-- IF NOT EXISTS so re-running against an environment that already has them is a
-- no-op (matches the rest of this folder's forward-only style).

CREATE TABLE IF NOT EXISTS custom_displays (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    fleet_id              UUID REFERENCES fleets(id) ON DELETE SET NULL,
    code                  TEXT NOT NULL UNIQUE,
    name                  VARCHAR(200) NOT NULL,
    advertiser_name       VARCHAR(200),
    status                VARCHAR(20) NOT NULL DEFAULT 'active',
    default_image_seconds INTEGER NOT NULL DEFAULT 8,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT custom_displays_status_check CHECK (status IN ('active', 'paused'))
);
CREATE INDEX IF NOT EXISTS ix_custom_displays_org  ON custom_displays(org_id);
CREATE INDEX IF NOT EXISTS ix_custom_displays_code ON custom_displays(code);

CREATE TABLE IF NOT EXISTS custom_display_items (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_id       UUID NOT NULL REFERENCES custom_displays(id) ON DELETE CASCADE,
    media_url        TEXT NOT NULL,
    media_type       VARCHAR(10) NOT NULL,
    duration_seconds INTEGER,
    position         INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT custom_display_items_media_type_check CHECK (media_type IN ('image', 'video'))
);
CREATE INDEX IF NOT EXISTS ix_custom_display_items_display ON custom_display_items(display_id);
