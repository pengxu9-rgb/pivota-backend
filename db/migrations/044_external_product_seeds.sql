-- External product seeds (employee-managed external products; redirect-only).
--
-- These records represent "first-class products" that can be surfaced in the
-- agent storefront (PLP/search/PDP) while redirecting purchases to an external URL.

CREATE TABLE IF NOT EXISTS external_product_seeds (
  id TEXT PRIMARY KEY,
  -- Stable "product_id" exposed to storefront for external products.
  -- Backfilled by application logic when missing.
  external_product_id TEXT NULL,
  market TEXT NOT NULL,
  tool TEXT NOT NULL DEFAULT '*',
  utm_template TEXT NULL,
  partner_type TEXT NULL,
  disclosure_text TEXT NULL,
  destination_url TEXT NOT NULL,
  canonical_url TEXT NULL,
  domain TEXT NULL,
  title TEXT NULL,
  image_url TEXT NULL,
  price_amount DOUBLE PRECISION NULL,
  price_currency TEXT NULL,
  availability TEXT NULL,
  seed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT NULL,
  created_by_employee_id TEXT NULL,
  attached_product_key TEXT NULL,
  attached_variant_id TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backward compatible: ensure new columns exist on older deployments.
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id TEXT;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS utm_template TEXT;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS partner_type TEXT;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS disclosure_text TEXT;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_status
  ON external_product_seeds(status);

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_attached
  ON external_product_seeds(attached_product_key, attached_variant_id);

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_domain
  ON external_product_seeds(domain);

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_created_at
  ON external_product_seeds(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_external_product_id
  ON external_product_seeds(external_product_id);

-- Enforce at most one active seed per (market, tool, external_product_id) when populated.
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_product_seeds_active_unique
  ON external_product_seeds(market, tool, external_product_id)
  WHERE status = 'active' AND external_product_id IS NOT NULL;

