-- 243: a category_path prefix index that covers the WHOLE catalog, not only internal_merchant rows.
--
-- 069's idx_catalog_products_category_path_active is partial (catalog_track = 'internal_merchant'
-- AND ...), but 23,811 of 25,434 catalog_products rows are external_seed (prod, 2026-09-26), so a
-- category-browse predicate `category_path = $x OR category_path LIKE 'beauty/haircare/%'` has no
-- index at all. The gateway's beauty mainline (PIVOTA-Agent canonicalCatalogSearch) evaluates that
-- predicate, ORed with its title/brand/recall_doc text arms, over catalog_products; with this index
-- every arm is indexable (the text arms already have trigram GIN indexes) and the scan becomes a
-- BitmapOr. Prod EXPLAIN ANALYZE with CANONICAL_CATALOG_CANDIDATE_KEY_PREFILTER on: that seq scan is
-- 191-619ms of a 0.65-1.7s query.
--
-- varchar_pattern_ops, like 069: category_path is varchar and the predicate is a left-anchored LIKE,
-- which a default-collation btree cannot serve; the opclass also serves the `=` arm.
--
-- CONCURRENTLY, like 206/214/237: the runner sends it on an AUTOCOMMIT connection, and it does not
-- block the catalog sync writers on a live table.
-- schema-guard-exempt: index only, no column added.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_category_path_pattern
  ON catalog_products (category_path varchar_pattern_ops);
