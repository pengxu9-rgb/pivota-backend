-- Commerce Index v2: source consent, field-level delta records, and staged publication jobs.
-- Canonical catalog tables continue to serve reads; this migration records why and
-- when a field changed, then lets workers publish only affected projections.

CREATE TABLE IF NOT EXISTS commerce_index_sources (
    source_id VARCHAR(255) PRIMARY KEY,
    merchant_id VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    integration_layer VARCHAR(32) NOT NULL,
    source_kind VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    consent_ref VARCHAR(255),
    capabilities_json JSONB,
    refresh_policy_json JSONB,
    source_config_json JSONB,
    last_success_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_commerce_index_sources_provider
    ON commerce_index_sources (merchant_id, provider, integration_layer);

CREATE TABLE IF NOT EXISTS commerce_index_field_changes (
    change_id VARCHAR(255) PRIMARY KEY,
    source_id VARCHAR(255) NOT NULL REFERENCES commerce_index_sources(source_id),
    merchant_id VARCHAR(64) NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_id VARCHAR(255) NOT NULL,
    field_path VARCHAR(192) NOT NULL,
    source_system VARCHAR(64) NOT NULL,
    source_ref VARCHAR(255),
    previous_fingerprint VARCHAR(64),
    value_fingerprint VARCHAR(64) NOT NULL,
    confidence DOUBLE PRECISION,
    observed_at TIMESTAMPTZ NOT NULL,
    fresh_until TIMESTAMPTZ,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_commerce_index_field_changes_entity
    ON commerce_index_field_changes (merchant_id, entity_type, entity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS commerce_index_publication_jobs (
    job_id VARCHAR(255) PRIMARY KEY,
    change_id VARCHAR(255) NOT NULL REFERENCES commerce_index_field_changes(change_id),
    merchant_id VARCHAR(64) NOT NULL,
    target VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    scope_json JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(128),
    claimed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    error_message TEXT,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (change_id, target)
);
CREATE INDEX IF NOT EXISTS idx_commerce_index_publication_jobs_pending
    ON commerce_index_publication_jobs (target, status, created_at);

-- Product Insights is a reviewed publication lane. A delta may create a
-- refresh request, but never auto-publishes external/highlight copy.
CREATE TABLE IF NOT EXISTS commerce_index_insight_refresh_requests (
    request_id VARCHAR(255) PRIMARY KEY,
    change_id VARCHAR(255) NOT NULL UNIQUE REFERENCES commerce_index_field_changes(change_id),
    merchant_id VARCHAR(64) NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_id VARCHAR(255) NOT NULL,
    field_path VARCHAR(192) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending_review',
    review_policy VARCHAR(64) NOT NULL,
    source_evidence_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_commerce_index_insight_refresh_review
    ON commerce_index_insight_refresh_requests (status, created_at);

-- Price/inventory deltas do not create a synthetic cart. They invalidate the
-- cached commercial assumption and require the existing live quote flow at
-- checkout, retaining an audit record for the triggering fact.
CREATE TABLE IF NOT EXISTS commerce_index_checkout_validation_requests (
    request_id VARCHAR(255) PRIMARY KEY,
    change_id VARCHAR(255) NOT NULL UNIQUE REFERENCES commerce_index_field_changes(change_id),
    merchant_id VARCHAR(64) NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_id VARCHAR(255) NOT NULL,
    field_path VARCHAR(192) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'requires_live_quote',
    validation_policy VARCHAR(96) NOT NULL,
    source_evidence_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_commerce_index_checkout_validation
    ON commerce_index_checkout_validation_requests (merchant_id, status, created_at);
