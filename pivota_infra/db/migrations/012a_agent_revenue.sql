-- Migration 012a: Agent Revenue Schema - Phase 5
-- Date: 2025-11-03
-- Purpose: Add agent revenue tracking and split policies (Data Layer)

-- ============================================================================
-- Part 1: agent_revenue_policies - Define revenue split agreements
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_revenue_policies (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    merchant_id VARCHAR(50),  -- NULL = applies to all merchants (default policy)
    split_ratio DECIMAL(5,4) NOT NULL CHECK (split_ratio >= 0 AND split_ratio <= 1),
    currency VARCHAR(3) DEFAULT 'USD',
    min_transaction_amount DECIMAL(12,2) DEFAULT 0,
    max_transaction_amount DECIMAL(12,2),
    active_period_start TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    active_period_end TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    created_by VARCHAR(100),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(agent_id, merchant_id, currency)
);

CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_agent ON agent_revenue_policies(agent_id, is_active);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_merchant ON agent_revenue_policies(merchant_id) WHERE merchant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_active_period ON agent_revenue_policies(active_period_start, active_period_end) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_active ON agent_revenue_policies(is_active, agent_id);

COMMENT ON TABLE agent_revenue_policies IS '[Phase 5] Agent revenue sharing policies with merchant-specific or default split ratios';
COMMENT ON COLUMN agent_revenue_policies.split_ratio IS 'Agent revenue share (0.0000 to 1.0000), e.g., 0.0200 = 2% of transaction';
COMMENT ON COLUMN agent_revenue_policies.merchant_id IS 'NULL = default policy for all merchants, specific ID = override for that merchant';

-- ============================================================================
-- Part 2: agent_revenue_logs - Transaction-level revenue tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_revenue_logs (
    id SERIAL PRIMARY KEY,
    tx_id VARCHAR(100) NOT NULL,
    routing_log_id INTEGER REFERENCES routing_logs(id) ON DELETE SET NULL,
    agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE SET NULL,
    merchant_id VARCHAR(50),
    psp_used VARCHAR(50),
    transaction_amount DECIMAL(12,2) NOT NULL,
    agent_earned_amount DECIMAL(12,2) NOT NULL,
    split_ratio_applied DECIMAL(5,4),
    currency VARCHAR(3) DEFAULT 'USD',
    settlement_status VARCHAR(20) DEFAULT 'pending' CHECK (settlement_status IN ('pending', 'processing', 'settled', 'failed', 'cancelled')),
    settlement_batch_id VARCHAR(50),
    settled_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_agent ON agent_revenue_logs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_settlement ON agent_revenue_logs(settlement_status, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_tx ON agent_revenue_logs(tx_id);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_batch ON agent_revenue_logs(settlement_batch_id) WHERE settlement_batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_routing ON agent_revenue_logs(routing_log_id) WHERE routing_log_id IS NOT NULL;

COMMENT ON TABLE agent_revenue_logs IS '[Phase 5] Transaction-level revenue tracking for agents with settlement status';
COMMENT ON COLUMN agent_revenue_logs.agent_earned_amount IS 'Amount earned by agent based on split_ratio_applied';
COMMENT ON COLUMN agent_revenue_logs.settlement_batch_id IS 'Links to settlement batch for payout processing';

-- ============================================================================
-- Part 3: Create views for revenue analytics
-- ============================================================================

CREATE OR REPLACE VIEW agent_revenue_summary AS
SELECT 
    agent_id,
    currency,
    COUNT(*) as total_transactions,
    SUM(agent_earned_amount) as total_earned,
    SUM(agent_earned_amount) FILTER (WHERE settlement_status = 'settled') as settled_amount,
    SUM(agent_earned_amount) FILTER (WHERE settlement_status = 'pending') as pending_amount,
    AVG(split_ratio_applied) as avg_split_ratio,
    MIN(created_at) as first_transaction,
    MAX(created_at) as latest_transaction
FROM agent_revenue_logs
GROUP BY agent_id, currency;

COMMENT ON VIEW agent_revenue_summary IS '[Phase 5] Agent revenue summary by currency with settlement breakdown';

-- ============================================================================
-- Part 4: Insert default revenue policies for existing agents
-- ============================================================================

-- Insert default 1% revenue share for existing agents
INSERT INTO agent_revenue_policies (agent_id, merchant_id, split_ratio, currency, created_by)
SELECT 
    agent_id,
    NULL as merchant_id,  -- Default policy
    0.0100 as split_ratio,  -- 1% default
    'USD' as currency,
    'system_migration' as created_by
FROM agents
WHERE NOT EXISTS (
    SELECT 1 FROM agent_revenue_policies arp 
    WHERE arp.agent_id = agents.agent_id AND arp.merchant_id IS NULL
);

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_revenue_policies') THEN
        RAISE NOTICE '[Phase 5] ✅ agent_revenue_policies table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_revenue_logs') THEN
        RAISE NOTICE '[Phase 5] ✅ agent_revenue_logs table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.views WHERE table_name = 'agent_revenue_summary') THEN
        RAISE NOTICE '[Phase 5] ✅ agent_revenue_summary view created';
    END IF;
END $$;
-- Date: 2025-11-03
-- Purpose: Add agent revenue tracking and split policies (Data Layer)

-- ============================================================================
-- Part 1: agent_revenue_policies - Define revenue split agreements
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_revenue_policies (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    merchant_id VARCHAR(50),  -- NULL = applies to all merchants (default policy)
    split_ratio DECIMAL(5,4) NOT NULL CHECK (split_ratio >= 0 AND split_ratio <= 1),
    currency VARCHAR(3) DEFAULT 'USD',
    min_transaction_amount DECIMAL(12,2) DEFAULT 0,
    max_transaction_amount DECIMAL(12,2),
    active_period_start TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    active_period_end TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    created_by VARCHAR(100),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(agent_id, merchant_id, currency)
);

CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_agent ON agent_revenue_policies(agent_id, is_active);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_merchant ON agent_revenue_policies(merchant_id) WHERE merchant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_active_period ON agent_revenue_policies(active_period_start, active_period_end) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_agent_revenue_policies_active ON agent_revenue_policies(is_active, agent_id);

COMMENT ON TABLE agent_revenue_policies IS '[Phase 5] Agent revenue sharing policies with merchant-specific or default split ratios';
COMMENT ON COLUMN agent_revenue_policies.split_ratio IS 'Agent revenue share (0.0000 to 1.0000), e.g., 0.0200 = 2% of transaction';
COMMENT ON COLUMN agent_revenue_policies.merchant_id IS 'NULL = default policy for all merchants, specific ID = override for that merchant';

-- ============================================================================
-- Part 2: agent_revenue_logs - Transaction-level revenue tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_revenue_logs (
    id SERIAL PRIMARY KEY,
    tx_id VARCHAR(100) NOT NULL,
    routing_log_id INTEGER REFERENCES routing_logs(id) ON DELETE SET NULL,
    agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE SET NULL,
    merchant_id VARCHAR(50),
    psp_used VARCHAR(50),
    transaction_amount DECIMAL(12,2) NOT NULL,
    agent_earned_amount DECIMAL(12,2) NOT NULL,
    split_ratio_applied DECIMAL(5,4),
    currency VARCHAR(3) DEFAULT 'USD',
    settlement_status VARCHAR(20) DEFAULT 'pending' CHECK (settlement_status IN ('pending', 'processing', 'settled', 'failed', 'cancelled')),
    settlement_batch_id VARCHAR(50),
    settled_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_agent ON agent_revenue_logs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_settlement ON agent_revenue_logs(settlement_status, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_tx ON agent_revenue_logs(tx_id);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_batch ON agent_revenue_logs(settlement_batch_id) WHERE settlement_batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_revenue_logs_routing ON agent_revenue_logs(routing_log_id) WHERE routing_log_id IS NOT NULL;

COMMENT ON TABLE agent_revenue_logs IS '[Phase 5] Transaction-level revenue tracking for agents with settlement status';
COMMENT ON COLUMN agent_revenue_logs.agent_earned_amount IS 'Amount earned by agent based on split_ratio_applied';
COMMENT ON COLUMN agent_revenue_logs.settlement_batch_id IS 'Links to settlement batch for payout processing';

-- ============================================================================
-- Part 3: Create views for revenue analytics
-- ============================================================================

CREATE OR REPLACE VIEW agent_revenue_summary AS
SELECT 
    agent_id,
    currency,
    COUNT(*) as total_transactions,
    SUM(agent_earned_amount) as total_earned,
    SUM(agent_earned_amount) FILTER (WHERE settlement_status = 'settled') as settled_amount,
    SUM(agent_earned_amount) FILTER (WHERE settlement_status = 'pending') as pending_amount,
    AVG(split_ratio_applied) as avg_split_ratio,
    MIN(created_at) as first_transaction,
    MAX(created_at) as latest_transaction
FROM agent_revenue_logs
GROUP BY agent_id, currency;

COMMENT ON VIEW agent_revenue_summary IS '[Phase 5] Agent revenue summary by currency with settlement breakdown';

-- ============================================================================
-- Part 4: Insert default revenue policies for existing agents
-- ============================================================================

-- Insert default 1% revenue share for existing agents
INSERT INTO agent_revenue_policies (agent_id, merchant_id, split_ratio, currency, created_by)
SELECT 
    agent_id,
    NULL as merchant_id,  -- Default policy
    0.0100 as split_ratio,  -- 1% default
    'USD' as currency,
    'system_migration' as created_by
FROM agents
WHERE NOT EXISTS (
    SELECT 1 FROM agent_revenue_policies arp 
    WHERE arp.agent_id = agents.agent_id AND arp.merchant_id IS NULL
);

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_revenue_policies') THEN
        RAISE NOTICE '[Phase 5] ✅ agent_revenue_policies table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_revenue_logs') THEN
        RAISE NOTICE '[Phase 5] ✅ agent_revenue_logs table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.views WHERE table_name = 'agent_revenue_summary') THEN
        RAISE NOTICE '[Phase 5] ✅ agent_revenue_summary view created';
    END IF;
END $$;
