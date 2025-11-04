-- ============================================================================
-- Migration 016: Add agent_type field for Agent Classification
-- ============================================================================
-- Purpose: Support tiered commission system (premium, standard, basic)
-- Created: 2025-11-03
-- Phase: 6
-- ============================================================================

-- Add agent_type column to agents table
ALTER TABLE agents 
ADD COLUMN IF NOT EXISTS agent_type VARCHAR(50) DEFAULT 'standard';

-- Create index for efficient queries
CREATE INDEX IF NOT EXISTS idx_agents_type 
ON agents(agent_type) 
WHERE agent_type IS NOT NULL;

-- Add comments
COMMENT ON COLUMN agents.agent_type IS 
'[Phase 6] Agent tier classification: premium, standard, basic, or custom. Used for matching commission offers from merchants.';

-- Set default value for existing agents
UPDATE agents 
SET agent_type = 'standard' 
WHERE agent_type IS NULL OR agent_type = '';

-- ============================================================================
-- Verification queries
-- ============================================================================

-- Check agent_type distribution
-- SELECT agent_type, COUNT(*) as count
-- FROM agents
-- GROUP BY agent_type
-- ORDER BY count DESC;

-- Check agents without type
-- SELECT COUNT(*) as agents_without_type
-- FROM agents
-- WHERE agent_type IS NULL OR agent_type = '';

COMMENT ON TABLE agents IS 'Agent entities with API keys, status, and tier classification for commission matching';

