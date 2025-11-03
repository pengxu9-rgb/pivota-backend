-- ============================================================================
-- Fix Agent Dashboard Data Issues
-- Date: 2025-10-27
-- ============================================================================

-- 1. Verify agent_id column exists
SELECT 
    column_name, 
    data_type, 
    is_nullable
FROM information_schema.columns 
WHERE table_name = 'orders' 
AND column_name IN ('agent_id', 'agent_session_id', 'metadata');

-- 2. Check recent orders and their agent_id values
SELECT 
    order_id, 
    agent_id, 
    merchant_id, 
    total, 
    payment_status,
    created_at,
    metadata
FROM orders
WHERE created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC
LIMIT 20;

-- 3. Check the newly created test order
SELECT 
    order_id, 
    agent_id, 
    merchant_id, 
    total, 
    payment_status,
    metadata
FROM orders
WHERE order_id = 'ORD_9BCAE1D751E0A461';

-- 4. Count orders by agent_id
SELECT 
    agent_id, 
    COUNT(*) as order_count,
    SUM(total) as total_revenue,
    AVG(total) as avg_order_value
FROM orders
WHERE agent_id IS NOT NULL
GROUP BY agent_id
ORDER BY order_count DESC;

-- 5. Check agent_usage_logs table
SELECT 
    agent_id,
    COUNT(*) as total_requests,
    COUNT(CASE WHEN endpoint LIKE '%/products/search%' THEN 1 END) as product_searches,
    COUNT(CASE WHEN endpoint LIKE '%/inventory%' THEN 1 END) as inventory_checks,
    COUNT(CASE WHEN endpoint LIKE '%/pricing%' THEN 1 END) as price_queries
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY agent_id;

-- ============================================================================
-- FIXES (Run only if issues confirmed)
-- ============================================================================

-- FIX 1: Add agent_id column if missing
ALTER TABLE orders 
ADD COLUMN IF NOT EXISTS agent_id VARCHAR(255);

-- FIX 2: Create index for better query performance
CREATE INDEX IF NOT EXISTS idx_orders_agent_id ON orders(agent_id);

-- FIX 3: Backfill agent_id from metadata (if stored there)
UPDATE orders
SET agent_id = (metadata->>'agent_id')::VARCHAR
WHERE agent_id IS NULL 
AND metadata IS NOT NULL 
AND metadata ? 'agent_id';

-- FIX 4: For test orders, manually set agent_id
-- (Only run if you know these orders belong to this agent)
-- UPDATE orders
-- SET agent_id = 'agent_ee38f2b3645a2ec2'
-- WHERE merchant_id = 'merch_208139f7600dbf42'
-- AND agent_id IS NULL
-- AND created_at > '2025-10-25';

-- ============================================================================
-- VERIFICATION QUERIES
-- ============================================================================

-- Verify Fix 1: Check orders for specific agent
SELECT 
    order_id, 
    agent_id, 
    total, 
    payment_status,
    created_at
FROM orders
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
ORDER BY created_at DESC;

-- Verify Fix 2: Check metrics data
SELECT 
    agent_id,
    COUNT(*) as total_orders,
    COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) as paid_orders,
    SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END) as total_paid_revenue,
    AVG(CASE WHEN payment_status = 'paid' THEN total END) as avg_paid_order_value
FROM orders
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY agent_id;

-- Verify Fix 3: Check merchants for agent
SELECT DISTINCT
    m.merchant_id,
    m.business_name,
    m.store_url,
    COUNT(o.order_id) as order_count,
    SUM(o.total) as total_revenue
FROM orders o
JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
WHERE o.agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY m.merchant_id, m.business_name, m.store_url;

