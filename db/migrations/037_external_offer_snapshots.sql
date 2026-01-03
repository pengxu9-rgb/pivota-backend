-- External offer snapshots (OG/JSON-LD) for external-only products.
-- Used to display title/image/price for links that can't be internally purchased.

CREATE TABLE IF NOT EXISTS external_offer_snapshots (
  id VARCHAR(64) PRIMARY KEY,
  market VARCHAR(8) NOT NULL,
  canonical_url TEXT NOT NULL,
  url_hash VARCHAR(64) NOT NULL,
  domain VARCHAR(256) NOT NULL,

  title TEXT,
  brand TEXT,
  image_url TEXT,
  price_amount NUMERIC,
  price_currency VARCHAR(8),
  availability VARCHAR(16) NOT NULL DEFAULT 'unknown',
  evidence JSONB,
  last_checked_at TIMESTAMPTZ,

  override_title TEXT,
  override_brand TEXT,
  override_image_url TEXT,
  override_price_amount NUMERIC,
  override_price_currency VARCHAR(8),

  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_offer_snapshots_market_hash
  ON external_offer_snapshots (market, url_hash);

