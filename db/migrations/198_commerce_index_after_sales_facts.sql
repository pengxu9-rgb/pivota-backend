-- Merchant-level return policy and after-sales experience are independent
-- facts. They must never be copied into a SKU as if every product shared the
-- same eligibility, nor inferred from an order/refund record.

CREATE TABLE IF NOT EXISTS commerce_index_merchant_after_sales_facts (
    fact_id VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    fact_kind VARCHAR(48) NOT NULL,
    market_code VARCHAR(16) NOT NULL DEFAULT 'unknown',
    policy_url TEXT,
    source_url TEXT NOT NULL,
    source_system VARCHAR(64) NOT NULL,
    source_ref VARCHAR(255),
    evidence_json JSONB NOT NULL,
    value_json JSONB NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    observed_at TIMESTAMPTZ NOT NULL,
    fresh_until TIMESTAMPTZ NOT NULL,
    review_required BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (fact_kind IN ('return_policy', 'after_sales_review_summary'))
);

-- One active fact per merchant/kind/market/source. A changed observation
-- supersedes the old row, preserving lineage instead of silently overwriting.
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_after_sales_active_source
  ON commerce_index_merchant_after_sales_facts (merchant_id, fact_kind, market_code, source_system, source_url)
  WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_after_sales_merchant_fresh
  ON commerce_index_merchant_after_sales_facts (merchant_id, fact_kind, fresh_until DESC)
  WHERE superseded_at IS NULL;

-- The graph publisher consumes a narrow, reviewed projection rather than raw
-- policy text or review bodies.
CREATE TABLE IF NOT EXISTS commerce_index_merchant_after_sales_publications (
    publication_id VARCHAR(255) PRIMARY KEY,
    fact_id VARCHAR(255) NOT NULL UNIQUE REFERENCES commerce_index_merchant_after_sales_facts(fact_id),
    merchant_id VARCHAR(64) NOT NULL,
    target VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending_review',
    projection_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (target IN ('relation_graph', 'merchant_insights'))
);
