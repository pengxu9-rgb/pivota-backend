-- Migration 011: Dual-Side Routing & AP2 Adapter - Phase 4++
-- Date: 2025-01-02
-- Purpose: Add merchant-side routing rules, AP2 protocol adapter integration, and routing conflict detection

-- ============================================================================
-- Part 1: routing_policies - Store merchant and agent routing rules
-- ============================================================================

CREATE TABLE IF NOT EXISTS routing_policies (
    id SERIAL PRIMARY KEY,
    owner_type VARCHAR(10) CHECK (owner_type IN ('merchant', 'agent')) NOT NULL,
    owner_id VARCHAR(50) NOT NULL,
    policy JSONB NOT NULL, -- {exclude: [], prefer: [], weights: {}, failover: []}
    is_active BOOLEAN DEFAULT true,
    priority INTEGER DEFAULT 1,
    created_by VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(owner_type, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_routing_policies_owner ON routing_policies(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_routing_policies_active ON routing_policies(is_active);

COMMENT ON TABLE routing_policies IS '[Phase 4++] Merchant and agent routing rules with priorities and preferences';
COMMENT ON COLUMN routing_policies.policy IS 'JSON structure: {exclude: ["stripe"], prefer: ["adyen"], weights: {"paypal": 0.8}, failover: ["square"]}';

-- ============================================================================
-- Part 2: routing_logs - Transaction-level routing trace
-- ============================================================================

CREATE TABLE IF NOT EXISTS routing_logs (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(50),
    agent_id VARCHAR(50),
    order_id VARCHAR(50),
    considered_psps JSONB, -- PSPs evaluated during routing
    chosen_psp VARCHAR(50),
    decision_trace JSONB, -- Full decision tree
    merchant_rules_applied JSONB,
    agent_rules_applied JSONB,
    conflict_detected BOOLEAN DEFAULT FALSE,
    resolution_method VARCHAR(50), -- merchant_priority, agent_whitelisted, default
    execution_time_ms INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_routing_logs_order ON routing_logs(order_id);
CREATE INDEX IF NOT EXISTS idx_routing_logs_merchant ON routing_logs(merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_routing_logs_agent ON routing_logs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_routing_logs_conflict ON routing_logs(conflict_detected) WHERE conflict_detected = true;

COMMENT ON TABLE routing_logs IS '[Phase 4++] Detailed trace of routing decisions including conflicts and resolutions';
COMMENT ON COLUMN routing_logs.resolution_method IS 'How conflicts were resolved: merchant_priority, agent_whitelisted, default';

-- ============================================================================
-- Part 3: ap2_transactions - AP2 protocol transaction logging
-- ============================================================================

CREATE TABLE IF NOT EXISTS ap2_transactions (
    id SERIAL PRIMARY KEY,
    routing_log_id INTEGER REFERENCES routing_logs(id) ON DELETE SET NULL,
    transaction_id VARCHAR(128) UNIQUE NOT NULL,
    order_id VARCHAR(50),
    agent_id VARCHAR(50),
    merchant_id VARCHAR(50),
    status VARCHAR(32) CHECK (status IN ('pending', 'authorized', 'captured', 'failed', 'refunded')),
    ap2_request JSONB,
    ap2_response JSONB,
    psp_used VARCHAR(50),
    amount DECIMAL(12,2),
    currency VARCHAR(3),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ap2_transactions_order ON ap2_transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_ap2_transactions_status ON ap2_transactions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ap2_transactions_agent ON ap2_transactions(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ap2_transactions_merchant ON ap2_transactions(merchant_id, created_at DESC);

COMMENT ON TABLE ap2_transactions IS '[Phase 4++] AP2 protocol transaction records linking to routing decisions';
COMMENT ON COLUMN ap2_transactions.ap2_request IS 'Original AP2 protocol request payload';
COMMENT ON COLUMN ap2_transactions.ap2_response IS 'AP2 protocol response including PSP response data';

-- ============================================================================
-- Part 4: Add governance whitelisting for agent overrides
-- ============================================================================

ALTER TABLE agents ADD COLUMN IF NOT EXISTS routing_override_enabled BOOLEAN DEFAULT FALSE;
COMMENT ON COLUMN agents.routing_override_enabled IS '[Phase 4++] Whether agent can override merchant routing rules';

-- ============================================================================
-- Part 5: Create initial routing policies for existing agents
-- ============================================================================

-- Insert default routing policies for existing agents
INSERT INTO routing_policies (owner_type, owner_id, policy)
SELECT 
    'agent' as owner_type,
    agent_id as owner_id,
    jsonb_build_object(
        'exclude', '[]'::jsonb,
        'prefer', '["stripe", "adyen"]'::jsonb,
        'weights', jsonb_build_object('stripe', 1.0, 'adyen', 0.9, 'paypal', 0.8),
        'failover', '["paypal", "square"]'::jsonb
    ) as policy
FROM agents
WHERE NOT EXISTS (
    SELECT 1 FROM routing_policies rp 
    WHERE rp.owner_type = 'agent' AND rp.owner_id = agents.agent_id
);

-- ============================================================================
-- Part 6: Create functions for routing conflict detection
-- ============================================================================

CREATE OR REPLACE FUNCTION detect_routing_conflicts(
    merchant_policy JSONB,
    agent_policy JSONB
) RETURNS JSONB AS $$
DECLARE
    conflict_report JSONB;
    merchant_excludes JSONB;
    agent_prefers JSONB;
    conflicts TEXT[] := '{}';
BEGIN
    merchant_excludes := COALESCE(merchant_policy->'exclude', '[]'::jsonb);
    agent_prefers := COALESCE(agent_policy->'prefer', '[]'::jsonb);
    
    -- Check if agent prefers a PSP that merchant excludes
    FOR i IN 0..jsonb_array_length(agent_prefers)-1 LOOP
        IF merchant_excludes @> jsonb_build_array(agent_prefers->i) THEN
            conflicts := array_append(conflicts, 
                format('Agent prefers %s but merchant excludes it', agent_prefers->i));
        END IF;
    END LOOP;
    
    conflict_report := jsonb_build_object(
        'has_conflict', array_length(conflicts, 1) > 0,
        'conflicts', to_jsonb(conflicts),
        'timestamp', now()
    );
    
    RETURN conflict_report;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION detect_routing_conflicts IS '[Phase 4++] Detects conflicts between merchant and agent routing policies';

-- ============================================================================
-- Part 7: Create views for routing analytics
-- ============================================================================

CREATE OR REPLACE VIEW routing_conflict_summary AS
SELECT 
    COUNT(*) FILTER (WHERE conflict_detected) as total_conflicts,
    COUNT(*) as total_routings,
    ROUND((COUNT(*) FILTER (WHERE conflict_detected)::numeric / NULLIF(COUNT(*), 0) * 100), 2) as conflict_rate,
    COUNT(DISTINCT merchant_id) as merchants_with_conflicts,
    COUNT(DISTINCT agent_id) as agents_with_conflicts,
    date_trunc('day', created_at) as date
FROM routing_logs
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY date_trunc('day', created_at)
ORDER BY date DESC;

COMMENT ON VIEW routing_conflict_summary IS '[Phase 4++] Daily summary of routing conflicts for monitoring';

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
BEGIN
    -- Verify all tables were created
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'routing_policies') THEN
        RAISE NOTICE '[Phase 4++] ✅ routing_policies table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'routing_logs') THEN
        RAISE NOTICE '[Phase 4++] ✅ routing_logs table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ap2_transactions') THEN
        RAISE NOTICE '[Phase 4++] ✅ ap2_transactions table created';
    END IF;
    
    -- Verify function was created
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'detect_routing_conflicts') THEN
        RAISE NOTICE '[Phase 4++] ✅ detect_routing_conflicts function created';
    END IF;
END $$;
