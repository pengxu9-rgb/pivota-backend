-- Fix existing agents that have NULL name/email
-- This updates any agents that are missing name/email data

-- First, let's see what we have
SELECT agent_id, name, email, api_key, status 
FROM agents 
WHERE name IS NULL OR email IS NULL OR name = '' OR email = '';

-- Update agents with NULL name - use agent_id as fallback
UPDATE agents 
SET name = COALESCE(name, 'Agent ' || SUBSTRING(agent_id FROM 7 FOR 8))
WHERE name IS NULL OR name = '';

-- Update agents with NULL email - this needs to be filled manually or from another source
-- For now, we'll use a placeholder that indicates it needs updating
UPDATE agents 
SET email = COALESCE(email, agent_id || '@agents.pivota.app')
WHERE email IS NULL OR email = '';

-- Verify the update
SELECT agent_id, name, email, status, created_at
FROM agents
ORDER BY created_at DESC;

