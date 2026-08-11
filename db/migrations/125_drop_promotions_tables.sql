-- 125: Drop the promotions lane (ADR-022).
--
-- The merchant-promotions feature is deleted end-to-end: it had zero production
-- usage — the prod `promotions` table held exactly 17 rows, all Shopify-synced
-- PIVOTA_AUDIT_20260421A/B fixtures for a single test merchant
-- (merch_efbc46b4619cfbdf), verified via the live API on 2026-08-11 before this
-- migration was written. `catalog_promotions` was a write-only mirror populated
-- by catalog_sync_service with no readers anywhere in the codebase.
--
-- The rows are recoverable from the pre-deletion commit's fixtures/scripts if
-- ever needed; the feature's rebuild design is recorded in ADR-022.

DROP TABLE IF EXISTS catalog_promotions;
DROP TABLE IF EXISTS promotions;
