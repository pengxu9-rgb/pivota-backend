-- ACP Delegate Payment & Webhook Outbox
-- Migration 026: Create tables for delegate tokens and webhook outbox

-- ============================================================================
-- Delegate Tokens (vt_...)
-- ============================================================================

CREATE TABLE IF NOT EXISTS delegate_tokens (
    token_id VARCHAR(50) PRIMARY KEY,
    
    -- Allowance constraints
    checkout_session_id VARCHAR(50) NOT NULL,
    merchant_id VARCHAR(50) NOT NULL,
    max_amount INTEGER NOT NULL,
    currency VARCHAR(3) NOT NULL,
    reason VARCHAR(20) NOT NULL DEFAULT 'one_time',
    
    -- Payment method details (JSONB for flexibility)
    payment_method JSONB,
    billing_address JSONB,
    risk_signals JSONB,
    metadata JSONB,
    
    -- Status tracking
    used BOOLEAN DEFAULT FALSE,
    used_at TIMESTAMP WITH TIME ZONE,
    used_by_session VARCHAR(50),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- Constraints
    CONSTRAINT fk_delegate_merchant
        FOREIGN KEY(merchant_id)
        REFERENCES merchant_onboarding(merchant_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_delegate_tokens_session ON delegate_tokens (checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_delegate_tokens_merchant ON delegate_tokens (merchant_id);
CREATE INDEX IF NOT EXISTS idx_delegate_tokens_expires ON delegate_tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_delegate_tokens_used ON delegate_tokens (used, expires_at);

-- ============================================================================
-- Webhook Outbox (for reliable event delivery)
-- ============================================================================

CREATE TABLE IF NOT EXISTS webhook_outbox (
    id SERIAL PRIMARY KEY,
    
    -- Event details
    event_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL,
    
    -- Target
    webhook_url TEXT,
    merchant_id VARCHAR(50),
    
    -- Delivery tracking
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    
    -- Response tracking
    response_status INTEGER,
    response_body TEXT,
    
    -- Correlation
    request_id VARCHAR(50),
    checkout_session_id VARCHAR(50),
    order_id VARCHAR(50),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    
    -- Constraints
    CONSTRAINT fk_webhook_merchant
        FOREIGN KEY(merchant_id)
        REFERENCES merchant_onboarding(merchant_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_webhook_session
        FOREIGN KEY(checkout_session_id)
        REFERENCES checkout_sessions(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_webhook_outbox_status ON webhook_outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_merchant ON webhook_outbox (merchant_id);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_session ON webhook_outbox (checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_created ON webhook_outbox (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_event ON webhook_outbox (event_type, created_at DESC);

-- ============================================================================
-- Cleanup Comments
-- ============================================================================

-- Clean old completed/failed webhook events (keep for 30 days)
-- DELETE FROM webhook_outbox WHERE completed_at < NOW() - INTERVAL '30 days';

-- Clean expired unused delegate tokens
-- DELETE FROM delegate_tokens WHERE expires_at < NOW() AND used = FALSE;

-- Consider setting up pg_cron or application-level cleanup


-- Migration 026: Create tables for delegate tokens and webhook outbox

-- ============================================================================
-- Delegate Tokens (vt_...)
-- ============================================================================

CREATE TABLE IF NOT EXISTS delegate_tokens (
    token_id VARCHAR(50) PRIMARY KEY,
    
    -- Allowance constraints
    checkout_session_id VARCHAR(50) NOT NULL,
    merchant_id VARCHAR(50) NOT NULL,
    max_amount INTEGER NOT NULL,
    currency VARCHAR(3) NOT NULL,
    reason VARCHAR(20) NOT NULL DEFAULT 'one_time',
    
    -- Payment method details (JSONB for flexibility)
    payment_method JSONB,
    billing_address JSONB,
    risk_signals JSONB,
    metadata JSONB,
    
    -- Status tracking
    used BOOLEAN DEFAULT FALSE,
    used_at TIMESTAMP WITH TIME ZONE,
    used_by_session VARCHAR(50),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- Constraints
    CONSTRAINT fk_delegate_merchant
        FOREIGN KEY(merchant_id)
        REFERENCES merchant_onboarding(merchant_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_delegate_tokens_session ON delegate_tokens (checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_delegate_tokens_merchant ON delegate_tokens (merchant_id);
CREATE INDEX IF NOT EXISTS idx_delegate_tokens_expires ON delegate_tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_delegate_tokens_used ON delegate_tokens (used, expires_at);

-- ============================================================================
-- Webhook Outbox (for reliable event delivery)
-- ============================================================================

CREATE TABLE IF NOT EXISTS webhook_outbox (
    id SERIAL PRIMARY KEY,
    
    -- Event details
    event_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL,
    
    -- Target
    webhook_url TEXT,
    merchant_id VARCHAR(50),
    
    -- Delivery tracking
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    
    -- Response tracking
    response_status INTEGER,
    response_body TEXT,
    
    -- Correlation
    request_id VARCHAR(50),
    checkout_session_id VARCHAR(50),
    order_id VARCHAR(50),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    
    -- Constraints
    CONSTRAINT fk_webhook_merchant
        FOREIGN KEY(merchant_id)
        REFERENCES merchant_onboarding(merchant_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_webhook_session
        FOREIGN KEY(checkout_session_id)
        REFERENCES checkout_sessions(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_webhook_outbox_status ON webhook_outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_merchant ON webhook_outbox (merchant_id);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_session ON webhook_outbox (checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_created ON webhook_outbox (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_event ON webhook_outbox (event_type, created_at DESC);

-- ============================================================================
-- Cleanup Comments
-- ============================================================================

-- Clean old completed/failed webhook events (keep for 30 days)
-- DELETE FROM webhook_outbox WHERE completed_at < NOW() - INTERVAL '30 days';

-- Clean expired unused delegate tokens
-- DELETE FROM delegate_tokens WHERE expires_at < NOW() AND used = FALSE;

-- Consider setting up pg_cron or application-level cleanup






