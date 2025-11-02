-- Migration 010: Payment Routing & Protocol Support - Phase 4
-- Date: 2025-11-02
-- Purpose: Add payment routing with multi-PSP failover and protocol support (AP2/ACP/X-402)

-- ============================================================================
-- Part 1: payment_routes - Define routing configuration per agent/merchant
-- ============================================================================

CREATE TABLE IF NOT EXISTS payment_routes (
    id SERIAL PRIMARY KEY,
    route_id VARCHAR(50) UNIQUE NOT NULL,
    agent_id VARCHAR(50),
    merchant_id VARCHAR(50),
    psp_priority JSONB DEFAULT '[]'::jsonb, -- [{"psp": "stripe", "priority": 1}, {"psp": "adyen", "priority": 2}]
    routing_strategy VARCHAR(30) DEFAULT 'priority', -- priority, cost, performance
    is_active BOOLEAN DEFAULT true,
    max_retries INTEGER DEFAULT 2,
    timeout_ms INTEGER DEFAULT 30000,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_payment_routes_agent ON payment_routes(agent_id, is_active);
CREATE INDEX IF NOT EXISTS idx_payment_routes_merchant ON payment_routes(merchant_id, is_active);
CREATE INDEX IF NOT EXISTS idx_payment_routes_active ON payment_routes(is_active);

COMMENT ON TABLE payment_routes IS 'Payment routing configuration with PSP priority and failover strategy';
COMMENT ON COLUMN payment_routes.psp_priority IS 'Ordered list of PSPs with priority (1=highest)';
COMMENT ON COLUMN payment_routes.routing_strategy IS 'priority=use order, cost=cheapest, performance=fastest';

-- ============================================================================
-- Part 2: payment_attempts - Log all payment attempts for monitoring
-- ============================================================================

CREATE TABLE IF NOT EXISTS payment_attempts (
    id SERIAL PRIMARY KEY,
    attempt_id VARCHAR(50) UNIQUE NOT NULL,
    order_id VARCHAR(50),
    route_id VARCHAR(50),
    agent_id VARCHAR(50),
    psp_name VARCHAR(50) NOT NULL,
    attempt_number INTEGER DEFAULT 1,
    status VARCHAR(30) NOT NULL, -- pending, success, failed, timeout, cancelled
    response_time_ms INTEGER,
    error_code VARCHAR(100),
    error_message TEXT,
    amount DECIMAL(12,2),
    currency VARCHAR(3),
    payment_method VARCHAR(50),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    
    FOREIGN KEY (route_id) REFERENCES payment_routes(route_id) ON DELETE SET NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_attempts_order ON payment_attempts(order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_attempts_route ON payment_attempts(route_id, status);
CREATE INDEX IF NOT EXISTS idx_payment_attempts_psp ON payment_attempts(psp_name, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_attempts_agent ON payment_attempts(agent_id, status, created_at DESC);

COMMENT ON TABLE payment_attempts IS 'Detailed log of all payment attempts including failovers';
COMMENT ON COLUMN payment_attempts.attempt_number IS 'Retry attempt count (1=first try, 2=first retry, etc)';

-- ============================================================================
-- Part 3: protocol_definitions - Define supported protocols
-- ============================================================================

CREATE TABLE IF NOT EXISTS protocol_definitions (
    id SERIAL PRIMARY KEY,
    protocol_name VARCHAR(30) NOT NULL, -- AP2, ACP, X-402
    version VARCHAR(10) NOT NULL,
    specification JSONB NOT NULL,
    endpoints JSONB DEFAULT '{}'::jsonb,
    required_fields JSONB DEFAULT '[]'::jsonb,
    validation_rules JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(20) DEFAULT 'active', -- active, deprecated, beta
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(protocol_name, version)
);

CREATE INDEX IF NOT EXISTS idx_protocol_defs_name ON protocol_definitions(protocol_name, status);
CREATE INDEX IF NOT EXISTS idx_protocol_defs_status ON protocol_definitions(status);

COMMENT ON TABLE protocol_definitions IS 'Protocol specifications for AP2, ACP, X-402';
COMMENT ON COLUMN protocol_definitions.specification IS 'Full protocol specification in JSON format';
COMMENT ON COLUMN protocol_definitions.endpoints IS 'Supported endpoints for this protocol';

-- ============================================================================
-- Part 4: protocol_events - Log protocol-specific events
-- ============================================================================

CREATE TABLE IF NOT EXISTS protocol_events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(50) UNIQUE NOT NULL,
    agent_id VARCHAR(50),
    protocol_name VARCHAR(30) NOT NULL,
    version VARCHAR(10),
    event_type VARCHAR(50) NOT NULL, -- request, response, error, validation_failure
    endpoint VARCHAR(255),
    payload JSONB,
    response JSONB,
    status VARCHAR(30) DEFAULT 'pending',
    response_time_ms INTEGER,
    error_details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_protocol_events_agent ON protocol_events(agent_id, protocol_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_protocol_events_protocol ON protocol_events(protocol_name, event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_protocol_events_status ON protocol_events(status, created_at DESC);

COMMENT ON TABLE protocol_events IS 'Event log for protocol-specific API calls';
COMMENT ON COLUMN protocol_events.event_type IS 'Type of protocol event (request, response, error, etc)';

-- ============================================================================
-- Part 5: psp_performance_metrics - Aggregate PSP performance data
-- ============================================================================

CREATE TABLE IF NOT EXISTS psp_performance_metrics (
    id SERIAL PRIMARY KEY,
    psp_name VARCHAR(50) NOT NULL,
    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    period_minutes INTEGER DEFAULT 5,
    total_attempts INTEGER DEFAULT 0,
    successful_attempts INTEGER DEFAULT 0,
    failed_attempts INTEGER DEFAULT 0,
    timeout_attempts INTEGER DEFAULT 0,
    avg_response_time_ms INTEGER,
    p95_response_time_ms INTEGER,
    p99_response_time_ms INTEGER,
    success_rate NUMERIC(5,2),
    total_amount DECIMAL(15,2),
    unique_merchants INTEGER DEFAULT 0,
    unique_agents INTEGER DEFAULT 0,
    failover_count INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(psp_name, period_start)
);

CREATE INDEX IF NOT EXISTS idx_psp_metrics_name ON psp_performance_metrics(psp_name, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_psp_metrics_period ON psp_performance_metrics(period_start DESC);

COMMENT ON TABLE psp_performance_metrics IS 'Aggregated PSP performance metrics for monitoring';

-- ============================================================================
-- Part 6: Insert default protocol definitions
-- ============================================================================

-- AP2 Protocol (Agent Payment Protocol v2)
INSERT INTO protocol_definitions (protocol_name, version, specification, endpoints, required_fields, status)
VALUES 
('AP2', '2.0', 
 '{"name": "Agent Payment Protocol v2", "type": "REST", "auth": "Bearer Token", "description": "Standard agent payment protocol with enhanced security"}'::jsonb,
 '{"create_payment": "/v2/payments", "get_status": "/v2/payments/{id}", "refund": "/v2/refunds"}'::jsonb,
 '["order_id", "amount", "currency", "merchant_id"]'::jsonb,
 'active'
),
('ACP', '1.0',
 '{"name": "Agent Commerce Protocol", "type": "REST+WebSocket", "auth": "API Key", "description": "Full commerce protocol with real-time updates"}'::jsonb,
 '{"order": "/acp/orders", "inventory": "/acp/inventory", "events": "wss://events/acp"}'::jsonb,
 '["agent_id", "merchant_id", "items", "customer"]'::jsonb,
 'active'
),
('X-402', '3.1',
 '{"name": "Extended Payment Protocol", "type": "REST", "auth": "OAuth2", "description": "Advanced payment protocol with multi-currency support"}'::jsonb,
 '{"authorize": "/x402/auth", "capture": "/x402/capture", "void": "/x402/void"}'::jsonb,
 '["transaction_id", "amount", "currency", "authorization_code"]'::jsonb,
 'beta'
)
ON CONFLICT (protocol_name, version) DO NOTHING;

-- ============================================================================
-- Part 7: Insert default routing configurations for existing agents
-- ============================================================================

-- Create default routes for existing agents
INSERT INTO payment_routes (route_id, agent_id, psp_priority, routing_strategy)
SELECT 
    CONCAT('route_', SUBSTRING(MD5(RANDOM()::TEXT), 1, 12)) as route_id,
    agent_id,
    '[{"psp": "stripe", "priority": 1}, {"psp": "adyen", "priority": 2}, {"psp": "paypal", "priority": 3}]'::jsonb as psp_priority,
    'priority' as routing_strategy
FROM agents
WHERE NOT EXISTS (
    SELECT 1 FROM payment_routes WHERE payment_routes.agent_id = agents.agent_id
)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- Part 8: Add protocol support to existing agents
-- ============================================================================

-- Update agent_protocols table with Phase 4 protocols
INSERT INTO agent_protocols (agent_id, protocol_name, version, status)
SELECT 
    a.agent_id,
    p.protocol_name,
    p.version,
    'active'
FROM agents a
CROSS JOIN (
    SELECT 'AP2' as protocol_name, '2.0' as version
    UNION ALL
    SELECT 'ACP', '1.0'
) p
WHERE NOT EXISTS (
    SELECT 1 FROM agent_protocols ap 
    WHERE ap.agent_id = a.agent_id 
    AND ap.protocol_name = p.protocol_name
)
ON CONFLICT (agent_id, protocol_name, version) DO NOTHING;

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'payment_routes') THEN
        RAISE NOTICE '✅ payment_routes table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'payment_attempts') THEN
        RAISE NOTICE '✅ payment_attempts table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'protocol_definitions') THEN
        RAISE NOTICE '✅ protocol_definitions table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'protocol_events') THEN
        RAISE NOTICE '✅ protocol_events table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'psp_performance_metrics') THEN
        RAISE NOTICE '✅ psp_performance_metrics table created';
    END IF;
END $$;
