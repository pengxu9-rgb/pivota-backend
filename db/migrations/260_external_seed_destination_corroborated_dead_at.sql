-- 260_external_seed_destination_corroborated_dead_at.sql
--
-- The clock that spaces a destination failure streak's steps.
--
-- services/external_seed_destination_liveness retires a seed after two CORROBORATED
-- confirmed-dead observations at least RETIREMENT_MIN_GAP (24h) apart. It used to measure
-- that gap from `destination_checked_at`, which EVERY observation that reached the origin
-- stamps -- including the refresh route's uncorroborated 404s, which run at 05:15Z over
-- ~10k seeds. The sweep (03:15Z) therefore almost always found a check under 24h old and held
-- the streak. Prod 2026-10-09: 157 of 160 active streak-1 seeds were last stamped by the
-- refresh, 562 active seeds sat at dead_404, and auto-retirements were 0-12 a day.
--
-- Written when the streak steps, cleared when it resets. NULL with a streak > 0 means the
-- streak predates this column; the writer then falls back to destination_checked_at.
-- Nullable, no default: on Postgres 11+ this is a catalog-only change, no table rewrite.

ALTER TABLE IF EXISTS external_product_seeds
  ADD COLUMN IF NOT EXISTS destination_corroborated_dead_at TIMESTAMPTZ;

COMMENT ON COLUMN external_product_seeds.destination_corroborated_dead_at IS
  'When destination_failure_streak last stepped on a catalogue-corroborated confirmed-dead observation. The retirement gap is measured from here, never from destination_checked_at. NULL when the streak is 0.';

-- Rollback (manual):
-- ALTER TABLE IF EXISTS external_product_seeds
--   DROP COLUMN IF EXISTS destination_corroborated_dead_at;
