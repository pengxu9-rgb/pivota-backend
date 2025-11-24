-- ACP Checkout Sessions Persistence
-- Migration 025: Create tables for checkout sessions and idempotency tracking

-- ============================================================================
-- Checkout Sessions
-- ============================================================================

CREATE TABLE IF NOT EXISTS checkout_sessions (
    id VARCHAR(50) PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    platform VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    
    -- Session data (JSONB for flexibility)
    buyer JSONB,
    items JSONB NOT NULL,
    fulfillment_address JSONB,
    fulfillment_option_id VARCHAR(50),
    
    -- Quote snapshot (stored for GET retrieval)
    quote JSONB NOT NULL,
    
    -- Payment info
    payment_provider VARCHAR(20),
    payment_token TEXT,
    
    -- Order linkage
    order_id VARCHAR(50),
    order_permalink_url TEXT,
    
    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE,
    
    -- Constraints
    CONSTRAINT fk_merchant
        FOREIGN KEY(merchant_id)
        REFERENCES merchant_onboarding(merchant_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_order
        FOREIGN KEY(order_id)
        REFERENCES orders(order_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_checkout_sessions_merchant ON checkout_sessions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_checkout_sessions_status ON checkout_sessions (status);
CREATE INDEX IF NOT EXISTS idx_checkout_sessions_created ON checkout_sessions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_checkout_sessions_expires ON checkout_sessions (expires_at) WHERE expires_at IS NOT NULL;

-- ============================================================================
-- Idempotency Keys
-- ============================================================================

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key VARCHAR(255) PRIMARY KEY,
    endpoint VARCHAR(100) NOT NULL,
    
    -- Request fingerprint
    request_method VARCHAR(10) NOT NULL,
    request_path TEXT NOT NULL,
    request_body_hash VARCHAR(64),
    
    -- Response cache
    response_status INTEGER,
    response_body JSONB,
    response_headers JSONB,
    
    -- Session/order linkage
    checkout_session_id VARCHAR(50),
    order_id VARCHAR(50),
    
    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- Constraints
    CONSTRAINT fk_checkout_session
        FOREIGN KEY(checkout_session_id)
        REFERENCES checkout_sessions(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_idempotency_keys_expires ON idempotency_keys (expires_at);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_session ON idempotency_keys (checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_endpoint ON idempotency_keys (endpoint, created_at DESC);

-- ============================================================================
-- Triggers
-- ============================================================================

-- Auto-update updated_at for checkout_sessions
CREATE OR REPLACE FUNCTION update_checkout_sessions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS trigger_update_checkout_sessions_updated_at ON checkout_sessions;
CREATE TRIGGER trigger_update_checkout_sessions_updated_at
    BEFORE UPDATE ON checkout_sessions
    FOR EACH ROW
    EXECUTE FUNCTION update_checkout_sessions_updated_at();

-- ============================================================================
-- Cleanup Job (optional - run via cron or background task)
-- ============================================================================

-- Clean expired sessions (status != 'completed')
-- DELETE FROM checkout_sessions WHERE expires_at < NOW() AND status != 'completed';

-- Clean expired idempotency keys
-- DELETE FROM idempotency_keys WHERE expires_at < NOW();

-- Comment: Consider setting up pg_cron or application-level cleanup

