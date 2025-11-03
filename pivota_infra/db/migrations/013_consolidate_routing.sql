-- Migration 013: Consolidate Routing Systems
-- Date: 2025-11-03
-- Purpose: Migrate payment_routes to routing_policies and deprecate old system

-- ============================================================================
-- Part 1: Migrate existing payment_routes to routing_policies
-- ============================================================================

-- Convert payment_routes to routing_policies format
INSERT INTO routing_policies (owner_type, owner_id, policy, priority, is_active, created_at, updated_at)
SELECT 
    'agent' as owner_type,
    agent_id as owner_id,
    jsonb_build_object(
        'exclude', '[]'::jsonb,
        'prefer', psp_priority,
        'weights', jsonb_build_object(
            -- Convert psp_priority array to weights (first=1.0, second=0.9, etc)
            CASE WHEN jsonb_array_length(psp_priority) > 0 
                 THEN (SELECT jsonb_object_agg(
                     item->>'psp', 
                     1.0 - ((item->>'priority')::int - 1) * 0.1
                 ) FROM jsonb_array_elements(psp_priority) item)
                 ELSE '{}'::jsonb
            END
        ),
        'failover', '[]'::jsonb,
        'routing_strategy', routing_strategy
    ) as policy,
    1 as priority,  -- Default priority
    is_active,
    created_at,
    updated_at
FROM payment_routes
WHERE agent_id IS NOT NULL
AND NOT EXISTS (
    -- Don't duplicate if already exists in routing_policies
    SELECT 1 FROM routing_policies rp
    WHERE rp.owner_type = 'agent' 
    AND rp.owner_id = payment_routes.agent_id
);

-- ============================================================================
-- Part 2: Mark payment_routes as deprecated
-- ============================================================================

ALTER TABLE payment_routes ADD COLUMN IF NOT EXISTS deprecated BOOLEAN DEFAULT false;
ALTER TABLE payment_routes ADD COLUMN IF NOT EXISTS migrated_to_policy_id INTEGER;

UPDATE payment_routes SET deprecated = true, updated_at = NOW()
WHERE agent_id IS NOT NULL;

COMMENT ON COLUMN payment_routes.deprecated IS 'Marked as deprecated - use routing_policies instead';
COMMENT ON TABLE payment_routes IS '[Deprecated] Use routing_policies table for routing configuration';

-- ============================================================================
-- Part 3: Create migration tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS routing_migration_log (
    id SERIAL PRIMARY KEY,
    migration_type VARCHAR(50),
    old_table VARCHAR(50),
    new_table VARCHAR(50),
    records_migrated INTEGER,
    migration_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    notes TEXT
);

INSERT INTO routing_migration_log (migration_type, old_table, new_table, records_migrated, notes)
SELECT 
    'consolidate_routing',
    'payment_routes',
    'routing_policies',
    COUNT(*),
    'Migrated payment_routes to routing_policies in Migration 013'
FROM payment_routes
WHERE deprecated = true;

-- ============================================================================
-- Verification
-- ============================================================================

DO $$
DECLARE
    old_routes INTEGER;
    new_policies INTEGER;
    migrated INTEGER;
BEGIN
    SELECT COUNT(*) INTO old_routes FROM payment_routes WHERE deprecated = true;
    SELECT COUNT(*) INTO new_policies FROM routing_policies WHERE owner_type = 'agent';
    SELECT records_migrated INTO migrated FROM routing_migration_log WHERE migration_type = 'consolidate_routing' ORDER BY id DESC LIMIT 1;
    
    RAISE NOTICE '[Migration 013] ✅ payment_routes deprecated: %', old_routes;
    RAISE NOTICE '[Migration 013] ✅ routing_policies (agents): %', new_policies;
    RAISE NOTICE '[Migration 013] ✅ Records migrated: %', COALESCE(migrated, 0);
    
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'payment_routes' AND column_name = 'deprecated') THEN
        RAISE NOTICE '[Migration 013] ✅ Deprecation column added';
    END IF;
END $$;
