-- Migration 184: ap2_trusted_issuers — AP2 mandate authority registry (ADR-012)
-- Date: 2026-07-17
-- Purpose: The trusted-issuer registry the mandate authority layer checks. A
-- mandate (Intent/Cart/Payment) is only honored if its ISSUER DID — the user who
-- authorized the agent — is registered as trusted for the presenting agent.
--
-- Keyed by agent_id: each agent is operated on behalf of specific user(s), so
-- the issuer DIDs allowed to authorize it are an agent-scoped binding. (ADR-012
-- frames this at the "account" level; agent-scoped is the precise pilot form —
-- generalize to an account id when a canonical one exists.)
--
-- Enrollment for the pilot is ADMIN-PROVISIONED (an operator inserts rows via
-- the db/ap2_trusted_issuers.py helpers or SQL); self-serve proof-of-control
-- enrollment is a later slice. status supports revocation without deleting the
-- audit row. Idempotent.

-- schema-guard-exempt: ap2_trusted_issuers is an opt-in AP2 table. The AP2 router
-- is disabled in every environment (ENABLE_AP2_ROUTES=false); the whole AP2
-- schema is applied on demand via the admin migration runner before AP2 is
-- enabled, so this table needs no boot-time self-heal.

CREATE TABLE IF NOT EXISTS ap2_trusted_issuers (
    id BIGSERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    issuer_did TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    revoked_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB
);

-- One row per (agent, issuer); re-granting a revoked pair reactivates it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ap2_trusted_issuers_unique
    ON ap2_trusted_issuers (agent_id, issuer_did);

-- Lookup path: active issuers for a presenting agent.
CREATE INDEX IF NOT EXISTS idx_ap2_trusted_issuers_lookup
    ON ap2_trusted_issuers (agent_id, status);

COMMENT ON TABLE ap2_trusted_issuers IS
    'AP2 mandate authority registry (ADR-012): issuer DIDs trusted to authorize a given agent via signed mandates. Admin-provisioned for the pilot.';

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 184 completed - ap2_trusted_issuers ready';
END $$;
