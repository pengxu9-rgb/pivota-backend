-- 168_external_conversion_poll_state.sql
-- Tier-2 (T2-2b): per-merchant watermark for the read_orders polling floor.
--
-- The poller (services/external_conversion_poller.py) fetches Shopify orders
-- with updated_at_min = the merchant's last poll watermark, so each run only
-- sees new/updated orders instead of re-scanning the whole window (quota + cost).
-- One row per merchant.
--
-- Idempotent DDL. Prod skips the migration runner — apply via railway ssh /
-- admin (matches migrations 159/166/167) or rely on schema_guard's startup
-- self-heal (db/schema_guard.ensure_required_schema_light), which mirrors this.
BEGIN;

CREATE TABLE IF NOT EXISTS external_conversion_poll_state (
  merchant_id        TEXT PRIMARY KEY,
  last_polled_at     TIMESTAMPTZ,          -- watermark: next run's updated_at_min
  last_run_at        TIMESTAMPTZ,          -- when the poller last ran for this merchant
  last_closed_count  INTEGER NOT NULL DEFAULT 0,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
