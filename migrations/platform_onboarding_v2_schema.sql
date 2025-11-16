-- Platform Merchant Onboarding v2 Schema Migration
-- This migration is additive and does not modify existing v1 structures
-- Execute this on your PostgreSQL database before deploying v2 features

-- 1. Add platform_profile column to merchant_onboarding table
ALTER TABLE merchant_onboarding
  ADD COLUMN IF NOT EXISTS platform_profile JSONB NULL;

COMMENT ON COLUMN merchant_onboarding.platform_profile IS 
  'Platform Onboarding v2 metadata - side-car JSON for platform merchants';

-- 2. Create platform_import_tasks table for tracking catalog imports
CREATE TABLE IF NOT EXISTS platform_import_tasks (
  id SERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  source_type VARCHAR(50) NOT NULL, -- 'connector', 'report', 'unknown'
  connector VARCHAR(100), -- 'linnworks', 'channeladvisor', etc.
  status VARCHAR(50) NOT NULL DEFAULT 'pending',
  counts JSONB, -- {"total": int, "succeeded": int, "failed": int}
  error TEXT,
  saga_id VARCHAR(100), -- For distributed transaction tracking
  attempt INTEGER NOT NULL DEFAULT 0,
  next_run_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Add indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_platform_import_tasks_merchant_id 
  ON platform_import_tasks(merchant_id);

CREATE INDEX IF NOT EXISTS idx_platform_import_tasks_status 
  ON platform_import_tasks(status);

CREATE INDEX IF NOT EXISTS idx_platform_import_tasks_next_run 
  ON platform_import_tasks(next_run_at) 
  WHERE status IN ('pending', 'retry_scheduled');

-- Add comments for documentation
COMMENT ON TABLE platform_import_tasks IS 
  'Tracks catalog import jobs for Platform merchants (EPIC-2)';

COMMENT ON COLUMN platform_import_tasks.saga_id IS 
  'Links related tasks for distributed transaction compensation';

COMMENT ON COLUMN platform_import_tasks.counts IS 
  'Import statistics: {"total": 100, "succeeded": 98, "failed": 2}';

-- Verify the migration
DO $$ 
BEGIN
  IF EXISTS (
    SELECT 1 
    FROM information_schema.columns 
    WHERE table_name = 'merchant_onboarding' 
    AND column_name = 'platform_profile'
  ) THEN
    RAISE NOTICE '✓ platform_profile column exists';
  ELSE
    RAISE WARNING '✗ platform_profile column missing';
  END IF;

  IF EXISTS (
    SELECT 1 
    FROM information_schema.tables 
    WHERE table_name = 'platform_import_tasks'
  ) THEN
    RAISE NOTICE '✓ platform_import_tasks table exists';
  ELSE
    RAISE WARNING '✗ platform_import_tasks table missing';
  END IF;
END $$;

