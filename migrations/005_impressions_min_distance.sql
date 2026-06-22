-- 005_impressions_min_distance
-- Adds the LiDAR proximity column to impressions. The spend processor fills it
-- from the concurrent scene_observed sample's mean_distance_m at cost time; the
-- audience summary aggregates MIN(min_distance_m) into the dashboards' "Nearest
-- approach" / proximity panel. NULL means no scene data => the UI renders "—".
ALTER TABLE impressions
    ADD COLUMN IF NOT EXISTS min_distance_m NUMERIC(6, 2);
