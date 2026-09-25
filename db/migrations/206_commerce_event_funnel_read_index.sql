-- 206: bound merchant commerce-event funnel reads by merchant and recency.
--
-- The canonical event ledger previously had separate merchant and occurred_at
-- indexes. Dashboard reads filter by merchant and optionally by platform/store,
-- then scan newest-first with a hard limit. Matching composite indexes keep the
-- bounded API query from scanning or sorting an entire merchant/store history.

-- CONCURRENTLY keeps this rollout out of the latency-sensitive startup schema
-- guard and avoids blocking ledger writers on an established production table.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_interaction_events_merchant_occurred
  ON commerce_interaction_events(merchant_id, occurred_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_interaction_events_merchant_platform_occurred
  ON commerce_interaction_events(merchant_id, platform, occurred_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_interaction_events_merchant_store_occurred
  ON commerce_interaction_events(merchant_id, store_id, occurred_at DESC);
