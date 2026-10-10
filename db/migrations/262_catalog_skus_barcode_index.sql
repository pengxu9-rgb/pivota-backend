-- 262: a btree on catalog_skus.barcode, for intake identity's widened GTIN tier (F2, 2026-10-10).
--
-- services/intake_identity.py Tier-0a used to read catalog_products.gtin only, and the crawl lane
-- sets that column only for a product with ONE barcoded variant -- every multi-variant row keeps its
-- barcodes in catalog_skus.barcode alone (census 2026-10-10: 88 cross-seller keys share a variant
-- barcode and were never linked). Tier-0a now also matches `catalog_skus.barcode IN (<spellings>)`
-- (barcode_lookup_sql: the GTIN-14 and its zero-stripped 13/12/8-digit forms, because the column
-- stores the source's barcode as given). That runs once per incoming listing at every enabled intake
-- door; without this index it is a sequential scan of catalog_skus each time. The code does not need
-- the index to be correct, only to be cheap.
--
-- Partial, like 178's idx_catalog_products_gtin: most SKU rows carry no barcode, and the lookup's
-- IN-list of constants implies barcode IS NOT NULL, so the planner can use it.
--
-- CONCURRENTLY, like 206/214/237/243: the runner sends it on an AUTOCOMMIT connection, and it does
-- not block the crawl/sync SKU writers on a live table.
-- schema-guard-exempt: index only, no column added.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_barcode
  ON catalog_skus (barcode)
  WHERE barcode IS NOT NULL;
