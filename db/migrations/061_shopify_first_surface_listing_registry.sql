CREATE TABLE IF NOT EXISTS surface_listing_states (
  listing_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  surface VARCHAR(32) NOT NULL,
  listing_key VARCHAR(255) NOT NULL,
  canonical_product_id VARCHAR(64) NULL,
  canonical_variant_id VARCHAR(64) NULL,
  status VARCHAR(32) NOT NULL,
  last_exported_at TIMESTAMPTZ NULL,
  last_indexed_at TIMESTAMPTZ NULL,
  last_tradeable_at TIMESTAMPTZ NULL,
  latest_receipt_id VARCHAR(64) NULL,
  latest_error_id VARCHAR(64) NULL,
  metadata JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_surface_listing_states_lookup
  ON surface_listing_states (merchant_id, surface, listing_key);

CREATE TABLE IF NOT EXISTS surface_listing_receipts (
  receipt_id VARCHAR(64) PRIMARY KEY,
  listing_id VARCHAR(64) NOT NULL,
  merchant_id VARCHAR(50) NOT NULL,
  surface VARCHAR(32) NOT NULL,
  listing_key VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS surface_listing_errors (
  error_id VARCHAR(64) PRIMARY KEY,
  listing_id VARCHAR(64) NOT NULL,
  merchant_id VARCHAR(50) NOT NULL,
  surface VARCHAR(32) NOT NULL,
  listing_key VARCHAR(255) NOT NULL,
  error_code VARCHAR(128) NOT NULL,
  error_message VARCHAR(1024) NULL,
  payload JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
