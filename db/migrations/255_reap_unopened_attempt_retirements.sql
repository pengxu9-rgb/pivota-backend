-- Operator-only negative outcome receipts. No buyer contact or provider data.
-- Both key fences and this audit must commit together. Keep receipts for the
-- lifetime of their immutable key mappings; never age them into new purchases.
CREATE TABLE IF NOT EXISTS reap_unopened_attempt_retirements (
    receipt_id VARCHAR(32) PRIMARY KEY,
    agent_id VARCHAR(128) NOT NULL,
    agent_user_ref_hash VARCHAR(64) NOT NULL,
    native_key VARCHAR(128) NOT NULL,
    cart_key VARCHAR(128) NOT NULL,
    native_request_hash VARCHAR(64) NOT NULL,
    cart_request_hash VARCHAR(64) NOT NULL,
    authority_sha256 VARCHAR(64) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    operator_ref VARCHAR(128) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (agent_id, agent_user_ref_hash, native_key, cart_key),
    CHECK (native_key <> cart_key)
);
