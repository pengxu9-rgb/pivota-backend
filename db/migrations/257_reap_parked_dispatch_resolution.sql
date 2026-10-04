-- Operator resolution of a parked checkout create (services/reap_checkout_recovery.py).
-- Widens the append-only journal's vocabulary with an operator `resolved` event. No existing
-- journal row is rewritten. Self-heal twin: db/reap_continuation.ensure_continuation_schema.
ALTER TABLE reap_checkout_dispatch_events DROP CONSTRAINT IF EXISTS reap_checkout_dispatch_events_event_type_check;
ALTER TABLE reap_checkout_dispatch_events ADD CONSTRAINT reap_checkout_dispatch_events_event_type_check
    CHECK (event_type IN ('started','not_created','observed','resolved'));

-- One operator decision per parked dispatch key; opaque handles only, no buyer contact or URL.
CREATE TABLE IF NOT EXISTS reap_checkout_dispatch_resolution_audit (
    purchase_id VARCHAR(64) NOT NULL,
    dispatch_key VARCHAR(64) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    reap_checkout_id VARCHAR(128),
    checkout_id_source VARCHAR(32),
    resolved_state VARCHAR(32) NOT NULL,
    operator_ref VARCHAR(128) NOT NULL,
    evidence_source VARCHAR(64) NOT NULL,
    evidence_reference VARCHAR(128) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    expected_updated_at TIMESTAMPTZ NOT NULL,
    evidence_observed_at TIMESTAMPTZ NOT NULL,
    provider_base_url VARCHAR(255) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (purchase_id, dispatch_key),
    CHECK (outcome IN ('checkout_found','confirmed_not_created')),
    CHECK (checkout_id_source IN ('journal_observed','operator_supplied')),
    CHECK (evidence_source IN ('authenticated_reap_checkout_read','verified_reap_support_statement')),
    CHECK ((outcome = 'checkout_found' AND reap_checkout_id IS NOT NULL AND checkout_id_source IS NOT NULL
            AND resolved_state = 'awaiting_approval')
        OR (outcome = 'confirmed_not_created' AND reap_checkout_id IS NULL AND checkout_id_source IS NULL
            AND resolved_state = 'quoting'))
);
