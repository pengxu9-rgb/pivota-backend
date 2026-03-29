-- 059_catalog_pivot_search_indexes.sql
-- Search-performance indexes for direct /v1/pivot/* routes on production-scale catalog tables.
-- This migration is Postgres-oriented and assumes 058_catalog_core.sql is already applied.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_merchants_merchant_name_trgm
    ON catalog_merchants USING GIN (LOWER(COALESCE(merchant_name, '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_title_trgm
    ON catalog_products USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_brand_trgm
    ON catalog_products USING GIN (LOWER(COALESCE(brand, '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_source_product_id_lookup
    ON catalog_products (source_product_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_title_trgm
    ON catalog_skus USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_sku_trgm
    ON catalog_skus USING GIN (LOWER(COALESCE(sku, '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_source_variant_id_lookup
    ON catalog_skus (source_variant_id);
