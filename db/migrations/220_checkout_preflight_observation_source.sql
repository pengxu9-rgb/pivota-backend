-- 220: say WHERE each preflight observation came from, because the two sources answer different
-- questions and averaging them answers neither.
--
-- Rows written by live traffic are biased toward what agents actually ask for: a handful of hot
-- products, whatever a caller happened to request this week. Rows written by a corpus sweep
-- (scripts/measure_checkout_preflight.py) are unbiased and complete but describe the catalog
-- rather than demand. The enforcement decision needs both and must not confuse them — "the
-- merchants refuse 20% of what buyers ask for" and "the merchants refuse 20% of the catalog" are
-- different claims, and only one of them justifies arming a gate on the checkout path.
--
-- Mixing them silently is the same denominator mistake this gate has now made three times in
-- review: counting rows the question was never asked of, and reporting the result as an answer.
--
-- ADDITIVE AND DEFAULTED, so every row already in the table keeps its meaning: everything written
-- before this migration came from the request path, which is exactly what 'live' means.
-- NOTE this file is NOT what creates these columns on production. `web` deploys with
-- SKIP_HEAVY_STARTUP_INIT, so db/migrations/ never runs there; db/catalog.py's model plus
-- metadata.create_all builds the table, and db/schema_guard.py self-heals an existing one. All
-- three carry these columns. This file is for environments that do run migrations, and for the
-- dialect gate.
ALTER TABLE checkout_preflight_observations
  ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'live',
  ADD COLUMN IF NOT EXISTS run_id VARCHAR(64) NULL;

-- Excluding one bad run from a window is the query this index exists for; the report itself is
-- served by idx_checkout_preflight_obs_created_at, which already leads on the window predicate.
CREATE INDEX IF NOT EXISTS idx_checkout_preflight_obs_run
  ON checkout_preflight_observations (run_id);
