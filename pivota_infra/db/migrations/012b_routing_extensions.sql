-- Migration 012b: Routing Extensions - Phase 5
-- Date: 2025-11-03
-- Purpose: Add metadata columns for agent control and revenue tracking (Metadata Layer)

-- ============================================================================
-- Part 1: Extend routing_logs with resolution tracking
-- ============================================================================

ALTER TABLE routing_logs 
ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(30) DEFAULT 'consensus';

ALTER TABLE routing_logs 
ADD COLUMN IF NOT EXISTS revenue_calculated BOOLEAN DEFAULT false;

COMMENT ON COLUMN routing_logs.resolved_by IS '[Phase 5] How routing was resolved: merchant_rule, agent_override, consensus, agent_whitelisted';
COMMENT ON COLUMN routing_logs.revenue_calculated IS '[Phase 5] Whether revenue split has been calculated for this routing';

-- Create index for revenue processing queries
CREATE INDEX IF NOT EXISTS idx_routing_logs_revenue ON routing_logs(revenue_calculated, created_at) WHERE revenue_calculated = false;

-- ============================================================================
-- Part 2: Extend agents table with revenue sharing flag
-- ============================================================================

ALTER TABLE agents 
ADD COLUMN IF NOT EXISTS revenue_sharing_enabled BOOLEAN DEFAULT false;

COMMENT ON COLUMN agents.revenue_sharing_enabled IS '[Phase 5] Whether agent participates in revenue sharing program';

-- ============================================================================
-- Part 3: Create function to update resolved_by based on decision trace
-- ============================================================================

CREATE OR REPLACE FUNCTION update_routing_resolved_by()
RETURNS TRIGGER AS $$
BEGIN
    -- Determine resolved_by from resolution_method
    IF NEW.resolution_method = 'merchant_priority' THEN
        NEW.resolved_by := 'merchant_rule';
    ELSIF NEW.resolution_method = 'agent_whitelisted' THEN
        NEW.resolved_by := 'agent_override';
    ELSIF NEW.resolution_method = 'consensus' THEN
        NEW.resolved_by := 'consensus';
    ELSE
        NEW.resolved_by := 'consensus';  -- Default
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger to automatically set resolved_by
CREATE TRIGGER set_routing_resolved_by
    BEFORE INSERT OR UPDATE ON routing_logs
    FOR EACH ROW
    EXECUTE FUNCTION update_routing_resolved_by();

COMMENT ON FUNCTION update_routing_resolved_by IS '[Phase 5] Auto-set resolved_by column based on resolution_method';

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
DECLARE
    resolved_by_exists BOOLEAN;
    revenue_calculated_exists BOOLEAN;
    revenue_sharing_exists BOOLEAN;
BEGIN
    -- Check if columns were added
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'routing_logs' AND column_name = 'resolved_by'
    ) INTO resolved_by_exists;
    
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'routing_logs' AND column_name = 'revenue_calculated'
    ) INTO revenue_calculated_exists;
    
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agents' AND column_name = 'revenue_sharing_enabled'
    ) INTO revenue_sharing_exists;
    
    IF resolved_by_exists THEN
        RAISE NOTICE '[Phase 5] ✅ routing_logs.resolved_by column added';
    END IF;
    
    IF revenue_calculated_exists THEN
        RAISE NOTICE '[Phase 5] ✅ routing_logs.revenue_calculated column added';
    END IF;
    
    IF revenue_sharing_exists THEN
        RAISE NOTICE '[Phase 5] ✅ agents.revenue_sharing_enabled column added';
    END IF;
    
    -- Verify trigger exists
    IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'set_routing_resolved_by') THEN
        RAISE NOTICE '[Phase 5] ✅ Auto-resolve trigger created';
    END IF;
END $$;
-- Date: 2025-11-03
-- Purpose: Add metadata columns for agent control and revenue tracking (Metadata Layer)

-- ============================================================================
-- Part 1: Extend routing_logs with resolution tracking
-- ============================================================================

ALTER TABLE routing_logs 
ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(30) DEFAULT 'consensus';

ALTER TABLE routing_logs 
ADD COLUMN IF NOT EXISTS revenue_calculated BOOLEAN DEFAULT false;

COMMENT ON COLUMN routing_logs.resolved_by IS '[Phase 5] How routing was resolved: merchant_rule, agent_override, consensus, agent_whitelisted';
COMMENT ON COLUMN routing_logs.revenue_calculated IS '[Phase 5] Whether revenue split has been calculated for this routing';

-- Create index for revenue processing queries
CREATE INDEX IF NOT EXISTS idx_routing_logs_revenue ON routing_logs(revenue_calculated, created_at) WHERE revenue_calculated = false;

-- ============================================================================
-- Part 2: Extend agents table with revenue sharing flag
-- ============================================================================

ALTER TABLE agents 
ADD COLUMN IF NOT EXISTS revenue_sharing_enabled BOOLEAN DEFAULT false;

COMMENT ON COLUMN agents.revenue_sharing_enabled IS '[Phase 5] Whether agent participates in revenue sharing program';

-- ============================================================================
-- Part 3: Create function to update resolved_by based on decision trace
-- ============================================================================

CREATE OR REPLACE FUNCTION update_routing_resolved_by()
RETURNS TRIGGER AS $$
BEGIN
    -- Determine resolved_by from resolution_method
    IF NEW.resolution_method = 'merchant_priority' THEN
        NEW.resolved_by := 'merchant_rule';
    ELSIF NEW.resolution_method = 'agent_whitelisted' THEN
        NEW.resolved_by := 'agent_override';
    ELSIF NEW.resolution_method = 'consensus' THEN
        NEW.resolved_by := 'consensus';
    ELSE
        NEW.resolved_by := 'consensus';  -- Default
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger to automatically set resolved_by
CREATE TRIGGER set_routing_resolved_by
    BEFORE INSERT OR UPDATE ON routing_logs
    FOR EACH ROW
    EXECUTE FUNCTION update_routing_resolved_by();

COMMENT ON FUNCTION update_routing_resolved_by IS '[Phase 5] Auto-set resolved_by column based on resolution_method';

-- ============================================================================
-- Migration verification
-- ============================================================================

DO $$
DECLARE
    resolved_by_exists BOOLEAN;
    revenue_calculated_exists BOOLEAN;
    revenue_sharing_exists BOOLEAN;
BEGIN
    -- Check if columns were added
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'routing_logs' AND column_name = 'resolved_by'
    ) INTO resolved_by_exists;
    
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'routing_logs' AND column_name = 'revenue_calculated'
    ) INTO revenue_calculated_exists;
    
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agents' AND column_name = 'revenue_sharing_enabled'
    ) INTO revenue_sharing_exists;
    
    IF resolved_by_exists THEN
        RAISE NOTICE '[Phase 5] ✅ routing_logs.resolved_by column added';
    END IF;
    
    IF revenue_calculated_exists THEN
        RAISE NOTICE '[Phase 5] ✅ routing_logs.revenue_calculated column added';
    END IF;
    
    IF revenue_sharing_exists THEN
        RAISE NOTICE '[Phase 5] ✅ agents.revenue_sharing_enabled column added';
    END IF;
    
    -- Verify trigger exists
    IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'set_routing_resolved_by') THEN
        RAISE NOTICE '[Phase 5] ✅ Auto-resolve trigger created';
    END IF;
END $$;
