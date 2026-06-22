# Migrations

Forward-only SQL patches applied to the Supabase Postgres instance, in
numeric order. Each file is the source of truth for one schema change so the
ORM (`src/kovio_cloud/models.py`) and the live database stay reproducible on a
fresh environment or a Supabase branch.

History:

- `001_initial_schema_with_money` and `002_users_table` predate this folder and
  were applied directly to Supabase; the ORM mirrors their result.
- `003_creative_type` lives on the `feat/video-image-creatives` branch.
- `004_campaigns_is_promo` — free-tier promo flag (this branch).
- `005_impressions_min_distance` — LiDAR proximity column (this branch).

All patches use `IF NOT EXISTS` so re-running against an environment that
already has the column (e.g. production, where some were applied by hand) is a
no-op.

Apply locally with `KOVIO_DEV_AUTO_CREATE_TABLES=true` (the dev stack calls
`Base.metadata.create_all`, which already includes these columns) or against a
remote project by running each file in order.
