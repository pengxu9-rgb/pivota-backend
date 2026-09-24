-- 237: index the two ways gmv_attribution_daily re-reads commerce_attribution_edges.
--
-- (merchant_id, created_at): a one-merchant recompute (a refund's recompute_days_for_edges, the
-- nightly stale-day sweep) reads one merchant's edges for one UTC day. The edges table had no index
-- on it (060, 065, 109), and the rollup's `(created_at AT TIME ZONE 'UTC')::date` equality could not
-- have used one, so each recompute scanned every edge of the merchant. _ROLLUP_QUERY now states the
-- day as a created_at range, which this index serves.
--
-- (updated_at): the stale-day sweep (gmv_aggregation_service.reroll_stale_days) starts from the
-- edges changed in its lookback window, a few days of a growing table.
--
-- CONCURRENTLY, like 206/214: the runner sends it on an AUTOCOMMIT connection, and it does not block
-- refund and payment writers on an established production table.
-- schema-guard-exempt: indexes only, no column added.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_attribution_edges_merchant_created
  ON commerce_attribution_edges (merchant_id, created_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_attribution_edges_updated_at
  ON commerce_attribution_edges (updated_at);
