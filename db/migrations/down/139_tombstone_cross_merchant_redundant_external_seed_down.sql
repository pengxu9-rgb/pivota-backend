-- Down for 139_tombstone_cross_merchant_redundant_external_seed.sql
--
-- Clears the suppression_reason set by the up migration. Safe to run only
-- when reverting the cleanup is intended — the underlying rows are still
-- cross-merchant duplicates and should be re-tombstoned (or properly merged)
-- before they can serve again.

BEGIN;

UPDATE catalog_products
SET suppression_reason = NULL,
    updated_at = now()
WHERE suppression_reason = 'cross_merchant_redundant_external_seed';

COMMIT;
