-- 223_external_seed_mirror_reconcile_indexes.sql
--
-- The external-seed → catalog_offers reconciler could not run at all.
--
-- scripts/reconcile_external_seed_offers.py is the designed repair for price /
-- availability / currency drift between `external_product_seeds` and the
-- `catalog_offers` mirror. Measured on prod 2026-09-16, its FIRST query
-- (MISSING_SQL) died on `canceling statement due to statement timeout`, so the
-- repair path had never executed even once.
--
-- It is not a data-volume problem. The tables are small — catalog_offers 33,091
-- rows, catalog_products 15,550, external_product_seeds 14,043. There was simply
-- NO index on `source_ref` on any of them, so the plan was three sequential
-- scans plus a nested loop whose join filter re-scanned catalog_products
-- (10,339 matching rows) once per survivor of the hash join (~11,315) — on the
-- order of 10^8 comparisons for a job that should touch a few thousand rows.
--
-- `source_ref` is the mirror's provenance key: catalog_products.source_ref and
-- catalog_offers.source_ref both hold the `external_product_seeds.id` the row
-- was projected from, under source_system = 'external_product_seeds_mirror_v1'.
-- Both reconciler joins are (source_ref, source_system), so that is the index.
--
-- Partial on `source_ref IS NOT NULL`: the column is nullable and most non-mirror
-- rows leave it null, so the partial keeps the index to the mirror cohort.
--
-- CONCURRENTLY is deliberately NOT used: this runner executes migrations inside a
-- transaction, and CREATE INDEX CONCURRENTLY cannot run there. These tables are
-- small enough that the brief lock is not worth a separate out-of-band step.

CREATE INDEX IF NOT EXISTS idx_catalog_products_source_ref_system
  ON catalog_products (source_ref, source_system)
  WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_catalog_offers_source_ref_system
  ON catalog_offers (source_ref, source_system)
  WHERE source_ref IS NOT NULL;
