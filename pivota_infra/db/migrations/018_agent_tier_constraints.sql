-- ============================================================================
-- Migration 018: Agent Tier Constraints (Basic/Premium)
-- ============================================================================
-- Purpose: Limit agent_type to 'basic' or 'premium', convert existing 'standard' to 'basic'
-- Created: 2025-11-05
-- Phase: 6.2
-- ============================================================================

-- Drop existing constraint if any
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_agent_type_check;

-- Convert existing 'standard' agents to 'basic'
UPDATE agents 
SET agent_type = 'basic' 
WHERE agent_type = 'standard' OR agent_type IS NULL;

-- Add constraint: only 'basic' or 'premium' allowed
ALTER TABLE agents 
ADD CONSTRAINT agents_agent_type_check 
CHECK (agent_type IN ('basic', 'premium'));

-- Set default to 'basic'
ALTER TABLE agents 
ALTER COLUMN agent_type SET DEFAULT 'basic';

-- Update comment
COMMENT ON COLUMN agents.agent_type IS 
  '[Phase 6.2] Agent tier: basic or premium. Premium agents may receive higher commission rates from merchants.';

-- ============================================================================
-- Verification
-- ============================================================================

DO $$
BEGIN
    -- Check distribution
    RAISE NOTICE '[Phase 6.2] Agent tier distribution:';
    
    DECLARE
        basic_count INTEGER;
        premium_count INTEGER;
    BEGIN
        SELECT COUNT(*) INTO basic_count FROM agents WHERE agent_type = 'basic';
        SELECT COUNT(*) INTO premium_count FROM agents WHERE agent_type = 'premium';
        
        RAISE NOTICE '  Basic agents: %', basic_count;
        RAISE NOTICE '  Premium agents: %', premium_count;
    END;
END $$;

-- Sample query to verify
-- SELECT agent_id, agent_name, agent_type FROM agents ORDER BY agent_type, agent_id LIMIT 10;

