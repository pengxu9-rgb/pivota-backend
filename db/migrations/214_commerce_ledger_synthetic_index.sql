-- 214: the only rows a retention job deletes wholesale are synthetic probes.
-- A partial index keeps that sweep from scanning real commerce history.
--
-- CONCURRENTLY, like 206, keeps this rollout out of the startup schema guard
-- and avoids blocking ledger writers on an established production table.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_interaction_events_synthetic
  ON commerce_interaction_events (merchant_id, occurred_at DESC)
  WHERE synthetic IS TRUE;
