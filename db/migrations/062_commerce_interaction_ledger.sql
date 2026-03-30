CREATE TABLE IF NOT EXISTS commerce_interactions (
    interaction_id VARCHAR(64) PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    platform VARCHAR(32),
    surface VARCHAR(64),
    prompt_id VARCHAR(64),
    result_id VARCHAR(64),
    click_id VARCHAR(64),
    quote_id VARCHAR(64),
    checkout_id VARCHAR(64),
    order_id VARCHAR(64),
    refund_id VARCHAR(64),
    return_id VARCHAR(64),
    canonical_product_id VARCHAR(64),
    canonical_variant_id VARCHAR(64),
    trace_id VARCHAR(128),
    brief_id VARCHAR(128),
    session_id VARCHAR(128),
    buyer_id VARCHAR(128),
    latest_event_type VARCHAR(64),
    status VARCHAR(32),
    metadata JSONB,
    first_occurred_at TIMESTAMPTZ,
    last_occurred_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commerce_interactions_merchant ON commerce_interactions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_platform ON commerce_interactions(platform);
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_surface ON commerce_interactions(surface);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_click_id_unique ON commerce_interactions(click_id) WHERE click_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_quote_id_unique ON commerce_interactions(quote_id) WHERE quote_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_checkout_id_unique ON commerce_interactions(checkout_id) WHERE checkout_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_order_id_unique ON commerce_interactions(order_id) WHERE order_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_refund_id_unique ON commerce_interactions(refund_id) WHERE refund_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_return_id_unique ON commerce_interactions(return_id) WHERE return_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS commerce_interaction_events (
    event_id VARCHAR(64) PRIMARY KEY,
    interaction_id VARCHAR(64) NOT NULL REFERENCES commerce_interactions(interaction_id) ON DELETE CASCADE,
    merchant_id VARCHAR(50) NOT NULL,
    platform VARCHAR(32),
    surface VARCHAR(64),
    event_type VARCHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    canonical_product_id VARCHAR(64),
    canonical_variant_id VARCHAR(64),
    trace_id VARCHAR(128),
    brief_id VARCHAR(128),
    session_id VARCHAR(128),
    source VARCHAR(128),
    upstream_idempotency_key TEXT,
    actor_type VARCHAR(32),
    actor_id VARCHAR(128),
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_interaction ON commerce_interaction_events(interaction_id);
CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_merchant ON commerce_interaction_events(merchant_id);
CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_event_type ON commerce_interaction_events(event_type);
CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_occurred_at ON commerce_interaction_events(occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interaction_events_idempotency
    ON commerce_interaction_events(merchant_id, event_type, upstream_idempotency_key)
    WHERE upstream_idempotency_key IS NOT NULL;
