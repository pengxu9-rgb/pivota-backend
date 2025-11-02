-- Migration 008: Agents Advanced Schema - Phase 2
-- Date: 2025-11-02
-- Purpose: Add multi-key support, protocol tracking, and performance stats

-- ============================================================================
-- Part 1: agent_api_keys - Multiple API keys per agent with scopes
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_api_keys (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    key_id VARCHAR(50) UNIQUE NOT NULL,
    key_hash VARCHAR(255) NOT NULL,
    key_prefix VARCHAR(20) NOT NULL,
    scopes JSON DEFAULT '["orders:read", "products:read"]'::json,
    ip_whitelist JSON DEFAULT '[]'::json,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    last_used_at TIMESTAMP WITH TIME ZONE,
    last_rotated_at TIMESTAMP WITH TIME ZONE,
    created_by VARCHAR(100),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_agent_api_keys_agent_id ON agent_api_keys(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_api_keys_is_active ON agent_api_keys(is_active);
CREATE INDEX IF NOT EXISTS idx_agent_api_keys_key_id ON agent_api_keys(key_id);

COMMENT ON TABLE agent_api_keys IS 'Multiple API keys per agent with scopes and IP restrictions';
COMMENT ON COLUMN agent_api_keys.scopes IS 'JSON array of allowed scopes: orders:read, products:read, etc.';
COMMENT ON COLUMN agent_api_keys.ip_whitelist IS 'JSON array of allowed IP addresses, empty = allow all';

-- ============================================================================
-- Part 2: agent_protocols - Track supported protocols
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_protocols (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    protocol_name VARCHAR(50) NOT NULL,
    version VARCHAR(20),
    status VARCHAR(20) DEFAULT 'active',
    last_verified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
    UNIQUE(agent_id, protocol_name, version)
);

CREATE INDEX IF NOT EXISTS idx_agent_protocols_agent_id ON agent_protocols(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_protocols_status ON agent_protocols(status);

COMMENT ON TABLE agent_protocols IS 'Track which protocols each agent supports (REST, GraphQL, WebSocket)';
COMMENT ON COLUMN agent_protocols.status IS 'active, deprecated, disabled';

-- ============================================================================
-- Part 3: agent_performance_stats - Pre-aggregated daily metrics
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_performance_stats (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    period_end TIMESTAMP WITH TIME ZONE NOT NULL,
    total_requests INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    success_rate NUMERIC(5, 2) DEFAULT 0,
    avg_latency_ms INTEGER DEFAULT 0,
    total_gmv NUMERIC(12, 2) DEFAULT 0,
    total_orders INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
    UNIQUE(agent_id, period_start)
);

CREATE INDEX IF NOT EXISTS idx_agent_perf_stats_agent_id ON agent_performance_stats(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_perf_stats_period ON agent_performance_stats(period_start DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_perf_stats_unique ON agent_performance_stats(agent_id, period_start);

COMMENT ON TABLE agent_performance_stats IS 'Daily aggregated performance metrics for efficient dashboard queries';
COMMENT ON COLUMN agent_performance_stats.period_start IS 'Start of measurement period (typically midnight UTC)';

-- ============================================================================
-- Part 4: Migrate existing api_key to agent_api_keys
-- ============================================================================

-- For existing agents, create an entry in agent_api_keys for their current key
INSERT INTO agent_api_keys (
    agent_id, 
    key_id, 
    key_hash, 
    key_prefix,
    scopes,
    is_active,
    created_at
)
SELECT 
    agent_id,
    CONCAT('key_', SUBSTRING(MD5(RANDOM()::TEXT), 1, 12)) as key_id,
    MD5(api_key) as key_hash,
    SUBSTRING(api_key, 1, 12) || '...' as key_prefix,
    '["orders:read", "products:read", "orders:write"]'::json as scopes,
    true as is_active,
    created_at
FROM agents
WHERE api_key IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM agent_api_keys WHERE agent_api_keys.agent_id = agents.agent_id
    )
ON CONFLICT (key_id) DO NOTHING;

-- ============================================================================
-- Part 5: Add default protocols for existing agents
-- ============================================================================

-- Assume all existing agents support REST by default
INSERT INTO agent_protocols (agent_id, protocol_name, version, status, last_verified_at)
SELECT 
    agent_id,
    'REST' as protocol_name,
    '1.0' as version,
    'active' as status,
    NOW() as last_verified_at
FROM agents
WHERE NOT EXISTS (
    SELECT 1 FROM agent_protocols 
    WHERE agent_protocols.agent_id = agents.agent_id 
        AND protocol_name = 'REST'
)
ON CONFLICT (agent_id, protocol_name, version) DO NOTHING;

-- ============================================================================
-- Migration complete
-- ============================================================================

-- Verify tables were created
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_api_keys') THEN
        RAISE NOTICE '✅ agent_api_keys table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_protocols') THEN
        RAISE NOTICE '✅ agent_protocols table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_performance_stats') THEN
        RAISE NOTICE '✅ agent_performance_stats table created';
    END IF;
END $$;


