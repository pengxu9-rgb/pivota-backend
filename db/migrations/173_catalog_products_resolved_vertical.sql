-- Migration 173: durable catalog_products.resolved_vertical
-- (multi-vertical audit architecture, Phase 0).
--
-- The audit pipeline is becoming vertical-aware. The resolved vertical
-- (beauty | fashion | electronics | other) must be resolved ONCE and stored,
-- not re-inferred on every read in each repo — the Node serving layer
-- (PIVOTA-Agent inferCategoryKind) and this backend both need to read the same
-- value. This is the load-bearing durable spine of Phase 0; no vertical
-- persistence existed before. The write path going forward is
-- services/catalog_sync_service.py (mirrors the mig 151 category_kind pattern);
-- see services.vertical_profiles.resolve_vertical for the resolution logic.
--
-- Deterministic backfill covers beauty from the existing beauty category_path
-- prefix (Pivota's primary vertical — the bulk of the catalog). Everything else
-- is left NULL and filled go-forward by the sync write path / resolved live at
-- read-time — never guessed in SQL.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + re-runnable backfill.
BEGIN;

ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS resolved_vertical VARCHAR(16);

UPDATE catalog_products
   SET resolved_vertical = 'beauty'
 WHERE lower(category_path) LIKE 'beauty/%'
   AND resolved_vertical IS NULL;

-- resolved_vertical filters in vertical-aware serving / audit queries.
CREATE INDEX IF NOT EXISTS idx_catalog_products_resolved_vertical
    ON catalog_products (resolved_vertical);

COMMIT;
