-- 004_campaigns_is_promo
-- Adds the free-tier flag used by create_campaign + the spend processor.
-- An org's first campaign is flagged is_promo=TRUE: the processor records its
-- impressions (for reach/attention data) but moves no money and exempts it from
-- the balance gate. Idempotent so it is safe to re-run against an environment
-- where the column was already applied by hand (e.g. production).
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS is_promo BOOLEAN NOT NULL DEFAULT FALSE;
