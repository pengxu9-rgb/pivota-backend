-- 138_catalog_products_content_changed_at.sql
--
-- catalog_products.updated_at means "this row was touched", and internal
-- metadata writers legitimately bump it for sync_status, content_key,
-- lifecycle, provenance, audit, and monitoring changes. The public product
-- sitemap needs a different signal: "the PDP content Google can see changed".
-- pivota-agent-ui PR #223 stopped fabricating lastmod values when the backend
-- omits them; this migration gives the backend a truthful source column for
-- /api/canonical/products.last_modified.
--
-- content_changed_at is backfilled from updated_at as a best-effort estimate
-- because historical content-change timestamps do not exist. The trigger below
-- advances content_changed_at only when public PDP / JSON-LD content-bearing
-- catalog_products fields change:
--
--   title, description, brand, image_url, product_payload, category,
--   category_path, product_type, price_tier, material, care, size_guide,
--   tags, use_case_tags, lifestyle_tags.
--
-- When catalog_products grows, add a field to this trigger only if changing it
-- changes content rendered on agent.pivota.cc/products/{sig_id} or JSON-LD
-- structured data seen by crawlers. Keep internal identity, lifecycle,
-- provenance, confidence, source, suppression, readiness, freshness, created_at,
-- and updated_at fields out of this trigger so routine maintenance does not
-- poison sitemap freshness again.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS content_changed_at TIMESTAMP NOT NULL DEFAULT NOW();

-- Backfill on first migration application, and also when a runtime schema guard
-- pre-created the column before the migration ran. Re-applying after the trigger
-- exists will not clobber legitimate content_changed_at values.
UPDATE catalog_products
SET content_changed_at = updated_at
WHERE content_changed_at IS NULL
   OR NOT EXISTS (
      SELECT 1
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE t.tgname = 'trg_catalog_products_content_changed_at'
        AND c.relname = 'catalog_products'
        AND n.nspname = current_schema()
        AND NOT t.tgisinternal
   );

ALTER TABLE catalog_products
  ALTER COLUMN content_changed_at SET DEFAULT NOW(),
  ALTER COLUMN content_changed_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_catalog_products_content_changed_at
  ON catalog_products (content_changed_at DESC);

CREATE OR REPLACE FUNCTION catalog_products_bump_content_changed_at()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.title IS DISTINCT FROM OLD.title
     OR NEW.description IS DISTINCT FROM OLD.description
     OR NEW.brand IS DISTINCT FROM OLD.brand
     OR NEW.image_url IS DISTINCT FROM OLD.image_url
     OR NEW.product_payload IS DISTINCT FROM OLD.product_payload
     OR NEW.category IS DISTINCT FROM OLD.category
     OR NEW.category_path IS DISTINCT FROM OLD.category_path
     OR NEW.product_type IS DISTINCT FROM OLD.product_type
     OR NEW.price_tier IS DISTINCT FROM OLD.price_tier
     OR NEW.material IS DISTINCT FROM OLD.material
     OR NEW.care IS DISTINCT FROM OLD.care
     OR NEW.size_guide IS DISTINCT FROM OLD.size_guide
     OR NEW.tags IS DISTINCT FROM OLD.tags
     OR NEW.use_case_tags IS DISTINCT FROM OLD.use_case_tags
     OR NEW.lifestyle_tags IS DISTINCT FROM OLD.lifestyle_tags
  THEN
    NEW.content_changed_at := NOW();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_catalog_products_content_changed_at ON catalog_products;
CREATE TRIGGER trg_catalog_products_content_changed_at
  BEFORE UPDATE ON catalog_products
  FOR EACH ROW EXECUTE FUNCTION catalog_products_bump_content_changed_at();
