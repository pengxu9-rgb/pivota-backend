-- Migration: Add agent_policies table for governance
-- Date: 2025-10-31
-- Purpose: Enable lightweight agent behavior monitoring and quality control

CREATE TABLE IF NOT EXISTS agent_policies (
    agent_id VARCHAR(50) PRIMARY KEY,
    max_requests_per_minute INTEGER DEFAULT 100,
    max_error_rate FLOAT DEFAULT 0.1,  -- 10% error threshold
    status VARCHAR(20) DEFAULT 'active',  -- active, suspended, blocked
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_policies_status ON agent_policies(status);
CREATE INDEX IF NOT EXISTS idx_agent_policies_agent_id ON agent_policies(agent_id);

-- Add default policy for existing agents
INSERT INTO agent_policies (agent_id, max_requests_per_minute, max_error_rate, status)
SELECT agent_id, 100, 0.1, 'active'
FROM agents
WHERE agent_id NOT IN (SELECT agent_id FROM agent_policies)
ON CONFLICT (agent_id) DO NOTHING;

COMMENT ON TABLE agent_policies IS 'Agent governance policies and rate limits';
COMMENT ON COLUMN agent_policies.max_requests_per_minute IS 'Maximum requests allowed per minute';
COMMENT ON COLUMN agent_policies.max_error_rate IS 'Maximum error rate threshold (0.1 = 10%)';
COMMENT ON COLUMN agent_policies.status IS 'Policy status: active, suspended, blocked';

