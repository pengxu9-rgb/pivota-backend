CREATE TABLE IF NOT EXISTS canonical_products (
  canonical_product_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  platform VARCHAR(32) NOT NULL,
  platform_product_id VARCHAR(255) NOT NULL,
  title VARCHAR(512) NOT NULL,
  description TEXT NULL,
  brand VARCHAR(255) NULL,
  category VARCHAR(255) NULL,
  default_image_url TEXT NULL,
  status VARCHAR(32) NULL,
  orderable BOOLEAN NULL,
  currency VARCHAR(8) NULL,
  visible_attributes JSONB NULL,
  ingredient_ids JSONB NULL,
  standard_product_data JSONB NOT NULL,
  source_payload_hash VARCHAR(64) NOT NULL,
  source_recorded_at TIMESTAMPTZ NULL,
  expires_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_products_merchant_platform_product
  ON canonical_products (merchant_id, platform, platform_product_id);

CREATE TABLE IF NOT EXISTS canonical_variants (
  canonical_variant_id VARCHAR(64) PRIMARY KEY,
  canonical_product_id VARCHAR(64) NOT NULL,
  merchant_id VARCHAR(50) NOT NULL,
  platform VARCHAR(32) NOT NULL,
  platform_product_id VARCHAR(255) NOT NULL,
  platform_variant_id VARCHAR(255) NOT NULL,
  title VARCHAR(512) NOT NULL,
  sku VARCHAR(255) NULL,
  barcode VARCHAR(255) NULL,
  currency VARCHAR(8) NULL,
  option_values JSONB NULL,
  visible_option_labels JSONB NULL,
  image_url TEXT NULL,
  standard_variant_data JSONB NOT NULL,
  source_payload_hash VARCHAR(64) NOT NULL,
  source_recorded_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_variants_merchant_platform_variant
  ON canonical_variants (merchant_id, platform, platform_product_id, platform_variant_id);

CREATE INDEX IF NOT EXISTS idx_canonical_variants_product
  ON canonical_variants (canonical_product_id);

CREATE TABLE IF NOT EXISTS canonical_offers (
  canonical_offer_id VARCHAR(64) PRIMARY KEY,
  canonical_product_id VARCHAR(64) NOT NULL,
  canonical_variant_id VARCHAR(64) NOT NULL,
  merchant_id VARCHAR(50) NOT NULL,
  currency VARCHAR(8) NOT NULL,
  amount NUMERIC(10,2) NOT NULL,
  compare_at_amount NUMERIC(10,2) NULL,
  availability VARCHAR(32) NULL,
  orderable BOOLEAN NULL,
  checkout_url TEXT NULL,
  source_payload_hash VARCHAR(64) NOT NULL,
  source_recorded_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_offers_variant
  ON canonical_offers (canonical_variant_id);

CREATE TABLE IF NOT EXISTS canonical_inventory_snapshots (
  id BIGSERIAL PRIMARY KEY,
  canonical_product_id VARCHAR(64) NOT NULL,
  canonical_variant_id VARCHAR(64) NOT NULL,
  merchant_id VARCHAR(50) NOT NULL,
  quantity INTEGER NULL,
  availability VARCHAR(32) NULL,
  observed_at TIMESTAMPTZ NULL,
  source VARCHAR(128) NOT NULL,
  stale BOOLEAN NOT NULL DEFAULT FALSE,
  source_payload_hash VARCHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_canonical_inventory_variant_observed
  ON canonical_inventory_snapshots (canonical_variant_id, observed_at);

CREATE TABLE IF NOT EXISTS canonical_product_sources (
  id BIGSERIAL PRIMARY KEY,
  canonical_product_id VARCHAR(64) NOT NULL,
  canonical_variant_id VARCHAR(64) NULL,
  merchant_id VARCHAR(50) NOT NULL,
  platform VARCHAR(32) NOT NULL,
  platform_product_id VARCHAR(255) NOT NULL,
  platform_variant_id VARCHAR(255) NULL,
  source_type VARCHAR(64) NOT NULL,
  source_name VARCHAR(128) NOT NULL,
  source_recorded_at TIMESTAMPTZ NULL,
  payload_hash VARCHAR(64) NOT NULL,
  raw_payload JSONB NULL,
  is_primary BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_canonical_product_sources_lookup
  ON canonical_product_sources (merchant_id, platform, platform_product_id, platform_variant_id);
