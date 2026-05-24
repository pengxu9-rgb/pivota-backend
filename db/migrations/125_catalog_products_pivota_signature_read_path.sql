-- 125_catalog_products_pivota_signature_read_path.sql
--
-- Keep the public canonical PDP resolver fast for both exact sig lookup
-- and sitemap pagination. Migration 071 created the primary partial unique
-- index, but production fast-mode deploys can skip raw db/migrations; this
-- migration is an idempotent backstop for environments that apply migrations
-- out of band.

CREATE INDEX IF NOT EXISTS idx_catalog_products_pivota_signature_list
  ON catalog_products (pivota_signature_id)
  WHERE pivota_signature_id IS NOT NULL;
