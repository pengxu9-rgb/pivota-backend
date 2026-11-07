-- 279_external_seed_destination_dead_404_since.sql
--
-- When the current run of hard 404s on a seed's served URL began.
--
-- services/external_seed_destination_liveness hides a served card (reversibly, reason
-- external_seed_destination_dead_unconfirmed) when its link has answered 404 or 410 on two
-- observations at least 24h apart with no read between, on hosts whose catalogue cannot
-- corroborate it. The 24h cannot be measured from destination_checked_at: every refresh that
-- sees the 404 re-stamps it, so a row retried more often than daily would never show the gap.
--
-- Written by every observation that reached the origin: kept while the answer stays a 404/410,
-- set to the observation's time when a run starts, cleared by any other answer. An
-- unverifiable observation leaves it alone. NULL on a row at dead_404 means the run predates
-- this column; the writer then starts it at destination_checked_at, which is never earlier
-- than the real start, so the gap can only be measured short (a later hide, never an earlier one).
-- Nullable, no default: on Postgres 11+ this is a catalog-only change, no table rewrite.
--
-- APPLY BEFORE THE IMAGE THAT WRITES IT SERVES: the observation writer SELECTs and UPDATEs it.

ALTER TABLE IF EXISTS external_product_seeds
  ADD COLUMN IF NOT EXISTS destination_dead_404_since TIMESTAMPTZ;

COMMENT ON COLUMN external_product_seeds.destination_dead_404_since IS
  'When the current run of 404/410 answers on the served URL began. Cleared by any other answer from the origin. The unconfirmed-dead hide measures its 24h gap from here.';
