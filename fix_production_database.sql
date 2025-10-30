-- Fix Production Database Issues
-- 1. Add missing total_gmv column to agents table
-- 2. Fix request_id constraint issue in agent_usage_logs

-- Step 1: Add total_gmv column to agents table if it doesn't exist
ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_gmv NUMERIC(12,2) DEFAULT 0;

-- Step 2: Update the request_id constraint to allow NULL values
-- First, drop the existing unique constraint
ALTER TABLE agent_usage_logs DROP CONSTRAINT IF EXISTS agent_usage_logs_request_id_key;

-- Then allow NULLs for request_id (NULL values are not considered for unique constraint)
ALTER TABLE agent_usage_logs ALTER COLUMN request_id DROP NOT NULL;

-- Re-add unique constraint (NULLs will be allowed and won't conflict)
ALTER TABLE agent_usage_logs ADD CONSTRAINT agent_usage_logs_request_id_key UNIQUE (request_id);

-- Step 3: Clean up any existing empty string request_ids
UPDATE agent_usage_logs SET request_id = NULL WHERE request_id = '';

-- Step 4: Add missing columns to agents table (if they don't exist)
ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_orders INTEGER DEFAULT 0;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITH TIME ZONE;

-- Step 5: Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id_timestamp 
ON agent_usage_logs(agent_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_agents_agent_id 
ON agents(agent_id);

-- Step 6: Verify the fixes
SELECT column_name, data_type, is_nullable 
FROM information_schema.columns 
WHERE table_name = 'agents' 
AND column_name IN ('total_gmv', 'total_requests', 'total_orders', 'last_used_at');

SELECT constraint_name, constraint_type 
FROM information_schema.table_constraints 
WHERE table_name = 'agent_usage_logs' 
AND constraint_name = 'agent_usage_logs_request_id_key';
