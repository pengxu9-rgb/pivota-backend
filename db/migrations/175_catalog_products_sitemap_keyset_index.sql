-- 175_catalog_products_sitemap_keyset_index.sql
--
-- GET /api/canonical/products orders by
--   (content_changed_at DESC, pivota_signature_id ASC,
--    content_key ASC, product_key ASC)
-- and now supports keyset (cursor) pagination that seeks on that same key.
-- This composite partial index lets both OFFSET and cursor pages walk index
-- order instead of sorting every eligible row on each request. The predicate
-- mirrors the route's local-table filters (pivota_signature_id LIKE 'sig_%'
-- AND content_key IS NOT NULL) so the index stays small.
--
-- Migration 138 created a single-column content_changed_at index, but it was
-- never added to db/schema_guard.py, and Railway production deploys do not
-- run db/migrations/*.sql — so prod has had no index behind this sort at all.
-- schema_guard.py carries the same CREATE INDEX below for the prod-startup
-- apply; this file keeps dev / migration-harness environments in sync.

CREATE INDEX IF NOT EXISTS idx_catalog_products_sitemap_keyset
  ON catalog_products (
    content_changed_at DESC,
    pivota_signature_id ASC,
    content_key ASC,
    product_key ASC
  )
  WHERE pivota_signature_id LIKE 'sig_%' AND content_key IS NOT NULL;
