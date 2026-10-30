-- 251: one shared request budget per crawl-egress edge (db/crawl_egress_pacer.py, services/shopify_edge_pacer.py).
--
-- 2026-09-30: Shopify's shared edge throttled our ONE crawl-egress IP (Cloud NAT pivota-crawl-nat, 34.82.199.35)
-- across every shop at once. Each crawl job paces per HOST, and several jobs run as separate Cloud Run processes,
-- so nothing saw the aggregate rate the edge was counting. One row per bucket ('shopify_edge') holds the edge's
-- schedule: `next_free_epoch` is the DB-clock instant (Unix seconds) at which the next unreserved request slot
-- starts. A process leases N slots with ONE upsert that advances it by N / rate and spaces its own requests
-- locally, so the shared schedule never runs faster than `rate` across all processes. `leases` counts round-trips
-- (an observability counter, nothing reads it for a decision).
--
-- DOUBLE PRECISION seconds, not TIMESTAMPTZ: the lease is arithmetic on the schedule (GREATEST + span), and the
-- process converts it to its own monotonic clock with the DB clock the same statement returns.
--
-- Nothing reads or writes this table unless CRAWL_SHOPIFY_EDGE_PACER_ENABLED is on. Migrations do not self-apply
-- in prod; ensure_table() in db/crawl_egress_pacer.py runs the identical CREATE at first lease. This file is the
-- record, and what the SQL gates plan against.

CREATE TABLE IF NOT EXISTS crawl_egress_pacer (
  bucket           TEXT PRIMARY KEY,
  next_free_epoch  DOUBLE PRECISION NOT NULL,
  leases           BIGINT NOT NULL DEFAULT 0,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
