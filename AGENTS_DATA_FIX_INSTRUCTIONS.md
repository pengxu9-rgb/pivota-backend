## Fix for "Unnamed Agent" Issue

### Problem
The agents table has `name` and `email` columns, but existing agents in the database have NULL values in these columns.

### Root Cause
The agent was likely created before the `name` and `email` columns were added to the agents table, or the columns are nullable and weren't populated during creation.

### Solution

You need to run a SQL update on the production database. Here are two options:

---

## Option 1: Quick Fix via Railway CLI

```bash
# Connect to Railway PostgreSQL
railway connect

# Then run this SQL:
UPDATE agents 
SET 
  name = COALESCE(name, 'Agent ' || SUBSTRING(agent_id FROM 7 FOR 8)),
  email = COALESCE(email, agent_id || '@agents.pivota.app')
WHERE name IS NULL OR name = '' OR email IS NULL OR email = '';

# Verify:
SELECT agent_id, name, email, status FROM agents;
```

---

## Option 2: Via Railway Dashboard

1. Go to https://railway.app
2. Select your project
3. Click on the PostgreSQL service
4. Click "Data" tab
5. Run this SQL:

```sql
-- Update existing agents with NULL/empty name or email
UPDATE agents 
SET 
  name = COALESCE(name, 'Agent ' || SUBSTRING(agent_id FROM 7 FOR 8)),
  email = COALESCE(email, agent_id || '@agents.pivota.app')
WHERE name IS NULL OR name = '' OR email IS NULL OR email = '';

-- Check the results
SELECT agent_id, name, email, status, created_at FROM agents;
```

---

## Option 3: Add Migration File (Best Practice)

Create a new migration file in your backend:

**File**: `pivota_infra/db/migrations/008_fix_agents_data.sql`

```sql
-- Migration 008: Fix existing agents with NULL name/email
-- Date: 2025-11-01

-- Update agents with missing data
UPDATE agents 
SET 
  name = COALESCE(name, 'Agent ' || SUBSTRING(agent_id FROM 7 FOR 8)),
  email = COALESCE(email, agent_id || '@agents.pivota.app')
WHERE name IS NULL OR name = '' OR email IS NULL OR email = '';

-- Make columns NOT NULL (if they aren't already)
ALTER TABLE agents ALTER COLUMN name SET NOT NULL;
ALTER TABLE agents ALTER COLUMN email SET NOT NULL;

-- Add default values for future inserts
ALTER TABLE agents ALTER COLUMN name SET DEFAULT 'Unnamed Agent';
```

Then run the migration:
```bash
psql $DATABASE_URL < pivota_infra/db/migrations/008_fix_agents_data.sql
```

---

## Temporary Fix (For Testing)

If you want to manually update the specific agent showing in your screenshot:

```sql
UPDATE agents 
SET 
  name = 'Test Agent',
  email = 'test@agents.pivota.app'
WHERE agent_id = 'agent_ee38f2b3645a2ec2';
```

---

## Prevention for Future Agents

Update the agent creation code to ensure name/email are always provided.

In `employee_agents_management.py`, the backend should validate that name and email exist before insertion.

---

## Verification

After running the fix, refresh the Employee Portal agents page. You should see:
- ✅ Agent name displayed (e.g., "Agent ee38f2b3" or the custom name you set)
- ✅ Email displayed (e.g., "agent_ee38f2b3645a2ec2@agents.pivota.app" or custom email)

The modal should also display all information correctly.

