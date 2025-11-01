-- Migration 006: PSP Fields Constraints and Indexes
-- Ensures data integrity for psp_used and psp_id fields

-- ============================================================================
-- Step 1: Clean existing data (normalize case, fill missing values)
-- ============================================================================

-- Normalize psp_used to lowercase
UPDATE orders
SET psp_used = LOWER(psp_used)
WHERE psp_used IS NOT NULL AND psp_used != LOWER(psp_used);

-- Fill missing psp_id based on psp_used
UPDATE orders o
SET psp_id = mp.psp_id
FROM merchant_psps mp
WHERE o.merchant_id = mp.merchant_id
    AND o.psp_id IS NULL
    AND o.psp_used IS NOT NULL
    AND LOWER(o.psp_used) = LOWER(mp.provider)
    AND mp.status = 'active';

-- Fill missing psp_used based on psp_id
UPDATE orders o
SET psp_used = LOWER(mp.provider)
FROM merchant_psps mp
WHERE o.psp_id = mp.psp_id
    AND o.psp_used IS NULL;

-- ============================================================================
-- Step 2: Add constraints
-- ============================================================================

-- Ensure psp_used is lowercase
ALTER TABLE orders 
    ADD CONSTRAINT check_psp_used_lowercase 
    CHECK (psp_used IS NULL OR psp_used = LOWER(psp_used));

-- Ensure valid PSP provider names
ALTER TABLE orders 
    ADD CONSTRAINT check_psp_used_valid_provider 
    CHECK (psp_used IS NULL OR psp_used IN ('stripe', 'adyen', 'checkout', 'paypal', 'braintree'));

-- Ensure psp_id follows format (if not null)
ALTER TABLE orders 
    ADD CONSTRAINT check_psp_id_format 
    CHECK (psp_id IS NULL OR psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$');

-- ============================================================================
-- Step 3: Add indexes for performance
-- ============================================================================

-- Index on psp_used for quick filtering
CREATE INDEX IF NOT EXISTS idx_orders_psp_used ON orders(psp_used);

-- Index on psp_id for exact matching
CREATE INDEX IF NOT EXISTS idx_orders_psp_id ON orders(psp_id);

-- Composite index for merchant + PSP queries
CREATE INDEX IF NOT EXISTS idx_orders_merchant_psp_id ON orders(merchant_id, psp_id);
CREATE INDEX IF NOT EXISTS idx_orders_merchant_psp_used ON orders(merchant_id, psp_used);

-- Index for PSP metrics queries (with time filter)
CREATE INDEX IF NOT EXISTS idx_orders_psp_created_at ON orders(psp_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_psp_payment_status ON orders(psp_used, payment_status);

-- ============================================================================
-- Step 4: Add helpful views
-- ============================================================================

-- View for PSP data quality
CREATE OR REPLACE VIEW psp_data_quality AS
SELECT 
    COUNT(*) as total_orders,
    COUNT(CASE WHEN psp_used IS NULL THEN 1 END) as null_psp_used,
    COUNT(CASE WHEN psp_id IS NULL THEN 1 END) as null_psp_id,
    COUNT(CASE WHEN psp_used IS NULL OR psp_id IS NULL THEN 1 END) as incomplete_orders,
    COUNT(CASE WHEN psp_used IS NOT NULL AND psp_id IS NOT NULL THEN 1 END) as complete_orders,
    ROUND(100.0 * COUNT(CASE WHEN psp_used IS NOT NULL AND psp_id IS NOT NULL THEN 1 END) / NULLIF(COUNT(*), 0), 2) as completion_rate
FROM orders;

-- View for PSP usage distribution
CREATE OR REPLACE VIEW psp_usage_stats AS
SELECT 
    LOWER(psp_used) as psp_provider,
    COUNT(*) as order_count,
    COUNT(DISTINCT merchant_id) as merchant_count,
    COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) as successful_orders,
    COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END), 0) as total_volume,
    ROUND(100.0 * COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) / NULLIF(COUNT(*), 0), 2) as success_rate
FROM orders
WHERE psp_used IS NOT NULL
GROUP BY LOWER(psp_used)
ORDER BY order_count DESC;

-- ============================================================================
-- Step 5: Add comments for documentation
-- ============================================================================

COMMENT ON COLUMN orders.psp_used IS 'PSP provider name (lowercase): stripe, adyen, checkout, paypal, braintree';
COMMENT ON COLUMN orders.psp_id IS 'PSP configuration ID from merchant_psps table (format: psp_{provider}_{random})';

COMMENT ON CONSTRAINT check_psp_used_lowercase ON orders IS 'Ensures psp_used is always lowercase for consistency';
COMMENT ON CONSTRAINT check_psp_used_valid_provider ON orders IS 'Ensures psp_used is a valid PSP provider name';
COMMENT ON CONSTRAINT check_psp_id_format ON orders IS 'Ensures psp_id follows the standard format';

-- ============================================================================
-- Verification queries
-- ============================================================================

-- Check data quality
SELECT * FROM psp_data_quality;

-- Check PSP usage
SELECT * FROM psp_usage_stats;

-- Find any remaining issues
SELECT 
    'Case issues' as issue_type,
    COUNT(*) as count
FROM orders
WHERE psp_used IS NOT NULL AND psp_used != LOWER(psp_used)
UNION ALL
SELECT 
    'Invalid psp_id format' as issue_type,
    COUNT(*) as count
FROM orders
WHERE psp_id IS NOT NULL AND psp_id !~* '^psp_[a-z0-9]+_[a-z0-9]{12}$'
UNION ALL
SELECT 
    'Incomplete PSP data' as issue_type,
    COUNT(*) as count
FROM orders
WHERE (psp_used IS NULL OR psp_id IS NULL);

