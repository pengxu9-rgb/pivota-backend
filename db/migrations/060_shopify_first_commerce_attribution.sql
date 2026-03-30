CREATE TABLE IF NOT EXISTS surface_click_events (
  click_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(50) NULL,
  surface VARCHAR(64) NOT NULL,
  canonical_product_id VARCHAR(64) NULL,
  canonical_variant_id VARCHAR(64) NULL,
  prompt_cluster VARCHAR(128) NULL,
  rule_id VARCHAR(64) NULL,
  job_id VARCHAR(128) NULL,
  session_id VARCHAR(128) NULL,
  destination_url TEXT NULL,
  dest_domain VARCHAR(256) NULL,
  impression_count INTEGER NOT NULL DEFAULT 0,
  click_count INTEGER NOT NULL DEFAULT 0,
  first_impression_at TIMESTAMPTZ NULL,
  last_impression_at TIMESTAMPTZ NULL,
  first_click_at TIMESTAMPTZ NULL,
  last_click_at TIMESTAMPTZ NULL,
  user_agent TEXT NULL,
  ip VARCHAR(64) NULL,
  context JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_surface_click_events_surface_created
  ON surface_click_events (surface, created_at);

CREATE TABLE IF NOT EXISTS commerce_attribution_edges (
  edge_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  click_id VARCHAR(64) NULL,
  order_id VARCHAR(50) NOT NULL,
  surface VARCHAR(64) NULL,
  canonical_product_id VARCHAR(64) NULL,
  canonical_variant_id VARCHAR(64) NULL,
  prompt_cluster VARCHAR(128) NULL,
  latest_refund_id VARCHAR(64) NULL,
  refund_ids JSONB NULL,
  refund_count INTEGER NOT NULL DEFAULT 0,
  refunded_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
  checkout_started_at TIMESTAMPTZ NULL,
  latest_refund_at TIMESTAMPTZ NULL,
  metadata JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_attribution_edges_order
  ON commerce_attribution_edges (order_id);
