-- Down for 139_tombstone_cross_merchant_redundant_external_seed.sql
--
-- Clears the suppression set by the up migration. Safe to run only when
-- reverting the cleanup is intended — the underlying rows are still
-- cross-merchant duplicates and should be re-tombstoned (or properly merged)
-- before they can serve again.
--
-- CLEARS BOTH COLUMNS (P1a, #1648). `suppressed_at` is the gate column that
-- every SQL lane, IPS, recall, the by-key doors and the quote door read;
-- `suppression_reason` is only the label. This down migration used to clear
-- the label alone. That worked while the up migration set the label alone too
-- — but the 2026-07-30 backfill gave all 54 of these rows a `suppressed_at`,
-- so clearing the label now un-labels them and leaves every one gated. A
-- revert that silently does not revert, and it would drive the
-- `suppression_timestamp_without_reason` invariant from 0 to 54.
--
-- GUARDED ON suppression_metadata IS NULL. `scripts/step5_lane4_ownist_twin_cut.py`
-- writes the SAME reason string but stamps `suppression_metadata` with its
-- run_id; the up migration writes no metadata at all. Without this guard the
-- down migration would un-gate lane4's suppressions as collateral — which the
-- old label-only version did too, it just did it invisibly because the rows
-- stayed gated by their timestamp.

BEGIN;

UPDATE catalog_products
SET suppression_reason = NULL,
    suppressed_at = NULL,
    updated_at = now()
WHERE suppression_reason = 'cross_merchant_redundant_external_seed'
  AND suppression_metadata IS NULL;

COMMIT;
