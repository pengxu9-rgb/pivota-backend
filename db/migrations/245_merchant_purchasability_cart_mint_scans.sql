-- 245: the purchasability sweep's cart-mint population, cached (db/merchant_purchasability_cart_mint_scans.py).
--
-- The sweep's cart-mint lane pages every active seed (~23.8k rows with seed_data) through the cart minter.
-- Run on each of the sweep's 21 hourly runs, that is a full seed-table scan every hour on the 2-vCPU prod
-- primary, where DB load caused serving timeouts on 2026-09-26/27. The lane now scans at most once per
-- 05:00Z day and the other runs read the last complete scan from here. One row per attempt; `hosts` is
-- JSON text ([[host, market, seeds], ...]) so the same SQL runs on SQLite in tests.
--
-- Migrations do not self-apply in prod; ensure_table() runs the identical CREATE at first use. This file is
-- the record, and what the SQL gates plan against.

CREATE TABLE IF NOT EXISTS merchant_purchasability_cart_mint_scans (
  scan_id        TEXT PRIMARY KEY,
  scanned_at     TIMESTAMPTZ NOT NULL,
  complete       BOOLEAN NOT NULL,
  reason         TEXT,
  seeds_scanned  INTEGER NOT NULL DEFAULT 0,
  cart_seeds     INTEGER NOT NULL DEFAULT 0,
  elapsed_ms     INTEGER NOT NULL DEFAULT 0,
  hosts          TEXT NOT NULL
);
