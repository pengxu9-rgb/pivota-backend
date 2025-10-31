-- Migration: Add realtime query fields to merchant_stores
-- Date: 2025-10-31
-- Purpose: Enable hybrid product query (cache vs realtime merchant API)

-- Add fields for realtime query configuration
ALTER TABLE merchant_stores 
ADD COLUMN IF NOT EXISTS realtime_enabled BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS api_endpoint VARCHAR(500),
ADD COLUMN IF NOT EXISTS query_ttl_seconds INTEGER DEFAULT 600;

-- Add index for quick config lookup
CREATE INDEX IF NOT EXISTS idx_merchant_stores_realtime 
ON merchant_stores(merchant_id, realtime_enabled) 
WHERE realtime_enabled = TRUE;

-- Add comment
COMMENT ON COLUMN merchant_stores.realtime_enabled IS 'If true, query merchant API in realtime instead of cache';
COMMENT ON COLUMN merchant_stores.api_endpoint IS 'Merchant self-hosted API endpoint for realtime queries';
COMMENT ON COLUMN merchant_stores.query_ttl_seconds IS 'Cache TTL in seconds (default 600 = 10 minutes)';


