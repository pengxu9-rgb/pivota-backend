-- Platform Onboarding v2 Schema Verification
-- Run this to ensure schema is ready

-- 1. Check/Add platform_profile column
ALTER TABLE merchant_onboarding
  ADD COLUMN IF NOT EXISTS platform_profile JSONB NULL;

-- 2. Create platform_import_tasks table if not exists
CREATE TABLE IF NOT EXISTS platform_import_tasks (
  id SERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  source_type VARCHAR(50) NOT NULL,
  connector VARCHAR(100),
  status VARCHAR(50) NOT NULL DEFAULT 'pending',
  counts JSONB,
  error TEXT,
  saga_id VARCHAR(100),
  attempt INTEGER NOT NULL DEFAULT 0,
  next_run_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Add indexes for performance
CREATE INDEX IF NOT EXISTS idx_platform_import_tasks_merchant_id 
  ON platform_import_tasks(merchant_id);

CREATE INDEX IF NOT EXISTS idx_platform_import_tasks_status 
  ON platform_import_tasks(status);

-- 4. Verify schema
SELECT 
  column_name, 
  data_type, 
  is_nullable
FROM information_schema.columns
WHERE table_name = 'merchant_onboarding' 
  AND column_name = 'platform_profile';

SELECT 
  table_name,
  column_name,
  data_type
FROM information_schema.columns
WHERE table_name = 'platform_import_tasks'
ORDER BY ordinal_position;

