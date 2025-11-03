-- Migration 014: Dual-Sided Revenue Share - Phase 5.5
-- Date: 2025-11-03
-- Purpose: Add merchant commission offers and dual-sided matching

-- ============================================================================
-- Part 1: merchant_commission_offers - Merchant sets commission rates
-- ============================================================================

CREATE TABLE IF NOT EXISTS merchant_commission_offers (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    agent_type VARCHAR(50),  -- NULL = all agents, or 'premium', 'standard', 'basic'
    offered_commission_rate DECIMAL(5,4) NOT NULL CHECK (offered_commission_rate >= 0 AND offered_commission_rate <= 1),
    min_order_amount DECIMAL(12,2) DEFAULT 0,
    max_order_amount DECIMAL(12,2),
    currency VARCHAR(3) DEFAULT 'USD',
    is_active BOOLEAN DEFAULT true,
    valid_from TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    valid_until TIMESTAMP WITH TIME ZONE,
    created_by VARCHAR(100),
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchant_commission_merchant ON merchant_commission_offers(merchant_id, is_active);
CREATE INDEX IF NOT EXISTS idx_merchant_commission_agent_type ON merchant_commission_offers(agent_type) WHERE agent_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_merchant_commission_active ON merchant_commission_offers(is_active, valid_from, valid_until);

COMMENT ON TABLE merchant_commission_offers IS '[Phase 5.5] Merchant-defined commission rates for agents';
COMMENT ON COLUMN merchant_commission_offers.agent_type IS 'NULL = offer applies to all agents, specific type = only for that agent tier';
COMMENT ON COLUMN merchant_commission_offers.offered_commission_rate IS 'Commission rate merchant is willing to pay (0.0000-1.0000)';

-- ============================================================================
-- Part 2: Rename and extend agent_revenue_policies → agent_revenue_expectations
-- ============================================================================

-- Rename table to reflect dual-sided model
ALTER TABLE agent_revenue_policies RENAME TO agent_revenue_expectations;

-- Add new columns for dual-sided matching
ALTER TABLE agent_revenue_expectations 
ADD COLUMN IF NOT EXISTS expected_commission_rate DECIMAL(5,4);

ALTER TABLE agent_revenue_expectations 
ADD COLUMN IF NOT EXISTS min_acceptable_rate DECIMAL(5,4);

ALTER TABLE agent_revenue_expectations 
ADD COLUMN IF NOT EXISTS agent_type VARCHAR(50) DEFAULT 'standard';

-- Update existing records to set expected rate from split_ratio
UPDATE agent_revenue_expectations 
SET expected_commission_rate = split_ratio,
    min_acceptable_rate = split_ratio * 0.8  -- 80% of expected as minimum
WHERE expected_commission_rate IS NULL;

COMMENT ON TABLE agent_revenue_expectations IS '[Phase 5.5] Agent-defined revenue expectations for matching with merchant offers';
COMMENT ON COLUMN agent_revenue_expectations.expected_commission_rate IS 'Preferred commission rate agent wants to earn';
COMMENT ON COLUMN agent_revenue_expectations.min_acceptable_rate IS 'Minimum rate agent will accept (fallback threshold)';

-- ============================================================================
-- Part 3: revenue_matching_logs - Record matching decisions
-- ============================================================================

CREATE TABLE IF NOT EXISTS revenue_matching_logs (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(100) NOT NULL,
    routing_log_id INTEGER REFERENCES routing_logs(id) ON DELETE SET NULL,
    agent_id VARCHAR(50),
    merchant_id VARCHAR(50),
    merchant_offered_rate DECIMAL(5,4),
    agent_expected_rate DECIMAL(5,4),
    agent_minimum_rate DECIMAL(5,4),
    actual_commission_rate DECIMAL(5,4) NOT NULL,
    match_status VARCHAR(30) CHECK (match_status IN (
        'perfect_match',
        'merchant_offer_accepted',
        'agent_below_min',
        'fallback_platform',
        'no_rules'
    )),
    match_source VARCHAR(30) CHECK (match_source IN (
        'merchant_offer',
        'agent_expectation',
        'platform_default',
        'negotiated'
    )),
    platform_default_used BOOLEAN DEFAULT false,
    metadata JSONB DEFAULT '{}'::jsonb,
    matched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_revenue_matching_order ON revenue_matching_logs(order_id);
CREATE INDEX IF NOT EXISTS idx_revenue_matching_agent ON revenue_matching_logs(agent_id, matched_at DESC);
CREATE INDEX IF NOT EXISTS idx_revenue_matching_merchant ON revenue_matching_logs(merchant_id, matched_at DESC);
CREATE INDEX IF NOT EXISTS idx_revenue_matching_status ON revenue_matching_logs(match_status);

COMMENT ON TABLE revenue_matching_logs IS '[Phase 5.5] Revenue matching decision logs for dual-sided commission negotiation';
COMMENT ON COLUMN revenue_matching_logs.match_status IS 'Result of matching: perfect_match, merchant_offer_accepted, agent_below_min, fallback_platform';
COMMENT ON COLUMN revenue_matching_logs.match_source IS 'Which side determined the final rate';

-- ============================================================================
-- Part 4: Extend agent_revenue_logs with matching reference
-- ============================================================================

ALTER TABLE agent_revenue_logs 
ADD COLUMN IF NOT EXISTS matching_log_id INTEGER REFERENCES revenue_matching_logs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_agent_revenue_matching ON agent_revenue_logs(matching_log_id) WHERE matching_log_id IS NOT NULL;

-- ============================================================================
-- Part 5: Create views for revenue matching analytics
-- ============================================================================

CREATE OR REPLACE VIEW revenue_matching_summary AS
SELECT 
    match_status,
    match_source,
    COUNT(*) as total_matches,
    AVG(actual_commission_rate) as avg_commission,
    AVG(merchant_offered_rate) as avg_merchant_offer,
    AVG(agent_expected_rate) as avg_agent_expectation,
    COUNT(DISTINCT agent_id) as unique_agents,
    COUNT(DISTINCT merchant_id) as unique_merchants
FROM revenue_matching_logs
WHERE matched_at > NOW() - INTERVAL '30 days'
GROUP BY match_status, match_source;

COMMENT ON VIEW revenue_matching_summary IS '[Phase 5.5] Revenue matching analytics for monitoring commission negotiation effectiveness';

-- ============================================================================
-- Part 6: Insert platform default commission offers
-- ============================================================================

-- Insert default platform commission offers for testing
INSERT INTO merchant_commission_offers (
    merchant_id, agent_type, offered_commission_rate, 
    min_order_amount, currency, created_by, notes
) VALUES
    ('platform_default', NULL, 0.015, 0, 'USD', 'system_migration', 'Platform default 1.5% commission'),
    ('platform_default', 'premium', 0.025, 100, 'USD', 'system_migration', 'Premium agents get 2.5%'),
    ('platform_default', 'standard', 0.020, 50, 'USD', 'system_migration', 'Standard agents get 2.0%')
ON CONFLICT DO NOTHING;

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'merchant_commission_offers') THEN
        RAISE NOTICE '[Phase 5.5] ✅ merchant_commission_offers table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_revenue_expectations') THEN
        RAISE NOTICE '[Phase 5.5] ✅ agent_revenue_policies renamed to agent_revenue_expectations';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'revenue_matching_logs') THEN
        RAISE NOTICE '[Phase 5.5] ✅ revenue_matching_logs table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'agent_revenue_expectations' AND column_name = 'expected_commission_rate') THEN
        RAISE NOTICE '[Phase 5.5] ✅ Dual-sided columns added to agent_revenue_expectations';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.views WHERE table_name = 'revenue_matching_summary') THEN
        RAISE NOTICE '[Phase 5.5] ✅ revenue_matching_summary view created';
    END IF;
END $$;
