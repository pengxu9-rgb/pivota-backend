-- Migration 015: Agent Portal Settlement Layer - Phase 5.6
-- Date: 2025-11-03
-- Purpose: Add agent settlement tracking, integration logs, and extend existing tables (NON-DESTRUCTIVE)

-- ============================================================================
-- Part 1: agent_settlements - Track agent settlement/payout records
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_settlements (
    id SERIAL PRIMARY KEY,
    settlement_id VARCHAR(50) UNIQUE NOT NULL,
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    merchant_id VARCHAR(50),  -- NULL = settlement across all merchants
    settlement_period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    settlement_period_end TIMESTAMP WITH TIME ZONE NOT NULL,
    total_transactions INTEGER DEFAULT 0,
    total_revenue DECIMAL(12,2) DEFAULT 0,
    settlement_amount DECIMAL(12,2) NOT NULL,
    commission_rate_applied DECIMAL(5,4),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    payout_method VARCHAR(30),
    payout_reference VARCHAR(100),
    payout_date TIMESTAMP WITH TIME ZONE,
    calculation_details JSONB DEFAULT '{}'::jsonb,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_settlements_agent ON agent_settlements(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_settlements_status ON agent_settlements(status, payout_date);
CREATE INDEX IF NOT EXISTS idx_agent_settlements_period ON agent_settlements(settlement_period_start, settlement_period_end);

COMMENT ON TABLE agent_settlements IS '[Phase 5.6] Agent settlement records with payout tracking';
COMMENT ON COLUMN agent_settlements.calculation_details IS 'JSONB with breakdown: {revenue_logs: [], total_commission: X, merchant_breakdown: {}}';

-- ============================================================================
-- Part 2: agent_integration_logs - Track agent integration actions
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_integration_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    action_type VARCHAR(50) NOT NULL,  -- 'protocol_test', 'routing_decision', 'merchant_connection', 'settlement_calc'
    target_entity VARCHAR(100),  -- merchant_id, protocol_name, settlement_id
    status VARCHAR(20),  -- 'success', 'failed', 'pending'
    request_data JSONB,
    response_data JSONB,
    execution_time_ms INTEGER,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_integration_agent ON agent_integration_logs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_integration_action ON agent_integration_logs(action_type, status);
CREATE INDEX IF NOT EXISTS idx_agent_integration_target ON agent_integration_logs(target_entity) WHERE target_entity IS NOT NULL;

COMMENT ON TABLE agent_integration_logs IS '[Phase 5.6] Agent integration action logs for audit and debugging';

-- ============================================================================
-- Part 3: Extend existing tables (NON-DESTRUCTIVE)
-- ============================================================================

-- Extend agent_revenue_expectations (Phase 5.5 existing table)
ALTER TABLE agent_revenue_expectations 
ADD COLUMN IF NOT EXISTS auto_accept_offers BOOLEAN DEFAULT false;

ALTER TABLE agent_revenue_expectations 
ADD COLUMN IF NOT EXISTS last_updated_by VARCHAR(100);

ALTER TABLE agent_revenue_expectations
ADD COLUMN IF NOT EXISTS notes TEXT;

COMMENT ON COLUMN agent_revenue_expectations.auto_accept_offers IS '[Phase 5.6] Auto-accept merchant offers >= min_acceptable_rate';

-- Extend agent_protocols (Phase 4 existing table)
ALTER TABLE agent_protocols
ADD COLUMN IF NOT EXISTS protocol_config JSONB DEFAULT '{}'::jsonb;

ALTER TABLE agent_protocols
ADD COLUMN IF NOT EXISTS last_tested_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE agent_protocols
ADD COLUMN IF NOT EXISTS test_result JSONB;

COMMENT ON COLUMN agent_protocols.protocol_config IS '[Phase 5.6] Agent-specific protocol configuration (API keys, endpoints)';
COMMENT ON COLUMN agent_protocols.last_tested_at IS '[Phase 5.6] Last time protocol was tested by agent';

-- ============================================================================
-- Part 4: Create views for Agent Portal dashboards
-- ============================================================================

CREATE OR REPLACE VIEW agent_settlement_summary AS
SELECT 
    agent_id,
    COUNT(*) as total_settlements,
    SUM(settlement_amount) as total_settled,
    SUM(settlement_amount) FILTER (WHERE status = 'completed') as completed_amount,
    SUM(settlement_amount) FILTER (WHERE status = 'pending') as pending_amount,
    MAX(payout_date) as last_payout_date,
    AVG(commission_rate_applied) as avg_commission_rate
FROM agent_settlements
GROUP BY agent_id;

COMMENT ON VIEW agent_settlement_summary IS '[Phase 5.6] Agent settlement overview for dashboard';

-- ============================================================================
-- Part 5: Create function to generate settlement_id
-- ============================================================================

CREATE OR REPLACE FUNCTION generate_settlement_id(p_agent_id VARCHAR, p_period_end TIMESTAMP)
RETURNS VARCHAR AS $$
BEGIN
    RETURN 'settle_' || SUBSTRING(p_agent_id FROM 7 FOR 8) || '_' || TO_CHAR(p_period_end, 'YYYYMMDD');
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION generate_settlement_id IS '[Phase 5.6] Generate unique settlement ID based on agent and period';

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_settlements') THEN
        RAISE NOTICE '[Phase 5.6] ✅ agent_settlements table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_integration_logs') THEN
        RAISE NOTICE '[Phase 5.6] ✅ agent_integration_logs table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'agent_revenue_expectations' AND column_name = 'auto_accept_offers') THEN
        RAISE NOTICE '[Phase 5.6] ✅ agent_revenue_expectations extended';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'agent_protocols' AND column_name = 'protocol_config') THEN
        RAISE NOTICE '[Phase 5.6] ✅ agent_protocols extended';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.views WHERE table_name = 'agent_settlement_summary') THEN
        RAISE NOTICE '[Phase 5.6] ✅ agent_settlement_summary view created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'generate_settlement_id') THEN
        RAISE NOTICE '[Phase 5.6] ✅ generate_settlement_id function created';
    END IF;
END $$;
