-- Migration 021: AP2 Security Infrastructure
-- Date: 2025-11-09
-- Purpose: Add schema for AP2 protocol security features

-- Add public key column to agents table
ALTER TABLE agents ADD COLUMN IF NOT EXISTS public_key TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS x402_enabled BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN agents.public_key IS 'ES256/Ed25519 public key for AP2 signature verification';
COMMENT ON COLUMN agents.x402_enabled IS 'Whether agent can initiate X-402 stablecoin payments';

-- Agent consent tokens for delegated actions
CREATE TABLE IF NOT EXISTS agent_consents (
    consent_id VARCHAR(100) PRIMARY KEY,
    agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE CASCADE,
    user_id VARCHAR(50),
    scope JSONB NOT NULL,
    status VARCHAR(20) CHECK (status IN ('active', 'revoked', 'expired')) DEFAULT 'active',
    nonce_counter INTEGER DEFAULT 0,
    spending_limit DECIMAL(15,2),
    spent_amount DECIMAL(15,2) DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_agent_consents_agent ON agent_consents(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_consents_user ON agent_consents(user_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_consents_expires ON agent_consents(expires_at) WHERE status = 'active';

COMMENT ON TABLE agent_consents IS 'AP2 user consent tokens for delegated agent actions';

-- Nonce tracking for replay protection
CREATE TABLE IF NOT EXISTS nonce_tracker (
    nonce VARCHAR(128) PRIMARY KEY,
    agent_id VARCHAR(50),
    consent_id VARCHAR(100),
    used_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '1 hour'
);

CREATE INDEX IF NOT EXISTS idx_nonce_tracker_agent ON nonce_tracker(agent_id, used_at DESC);
CREATE INDEX IF NOT EXISTS idx_nonce_tracker_expires ON nonce_tracker(expires_at);

COMMENT ON TABLE nonce_tracker IS 'Nonce tracking for AP2 replay attack prevention';

-- Verification
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 021 completed - AP2 security infrastructure ready';
END $$;

