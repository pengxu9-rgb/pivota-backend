-- Immutable one-decision audit; no provider payload or buyer PII.
CREATE TABLE IF NOT EXISTS reap_checkout_manual_resolution_audit (
    purchase_id VARCHAR(64) PRIMARY KEY,
    reap_checkout_id VARCHAR(128) NOT NULL,
    from_state VARCHAR(32) NOT NULL,
    resolved_state VARCHAR(32) NOT NULL,
    attribution_outcome VARCHAR(32) NOT NULL,
    operator_ref VARCHAR(128) NOT NULL,
    evidence_source VARCHAR(64) NOT NULL,
    evidence_reference VARCHAR(128) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    expected_updated_at TIMESTAMPTZ NOT NULL,
    evidence_observed_at TIMESTAMPTZ NOT NULL,
    provider_base_url VARCHAR(255) NOT NULL,
    provider_status VARCHAR(32) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (from_state IN ('awaiting_approval','processing')),
    CHECK (resolved_state IN ('completed','failed','expired')),
    CHECK (provider_status IN ('COMPLETED','FAILED','EXPIRED')),
    CHECK (attribution_outcome IN ('edge_closed','closed_by_other_channel','not_applicable')),
    CHECK (evidence_source IN ('authenticated_reap_checkout_read','verified_reap_support_statement'))
);
