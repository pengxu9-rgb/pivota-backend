-- Down for 220. Dropping `source` loses the live/sweep distinction, which means the remaining
-- rows can no longer be read as either a demand rate or a catalog rate — so this is a schema
-- rollback, not a data-preserving one. Take a copy first if the window still matters.
DROP INDEX IF EXISTS idx_checkout_preflight_obs_run;
ALTER TABLE checkout_preflight_observations DROP COLUMN IF EXISTS run_id;
ALTER TABLE checkout_preflight_observations DROP COLUMN IF EXISTS source;
