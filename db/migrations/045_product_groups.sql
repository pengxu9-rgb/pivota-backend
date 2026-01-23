-- Product group mapping (multi-seller “one product + many offers”).
--
-- This table maps merchant-scoped platform products into a shared product_group_id.
-- The grouping can be curated/managed by internal tools and consumed by agent/gateway
-- to build offers and correctly route checkout to the selected seller.

CREATE TABLE IF NOT EXISTS product_group_members (
  product_group_id TEXT NOT NULL,
  merchant_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  platform_product_id TEXT NOT NULL,
  is_primary BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (merchant_id, platform, platform_product_id)
);

-- Backward compatible: ensure columns exist on older deployments.
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS product_group_id TEXT;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS merchant_id TEXT;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS platform TEXT;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS platform_product_id TEXT;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_product_group_members_group_id
  ON product_group_members(product_group_id);

CREATE INDEX IF NOT EXISTS idx_product_group_members_merchant_id
  ON product_group_members(merchant_id);
