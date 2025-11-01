-- Check Agent Metrics Issue
-- The agent has 149 total requests but metrics shows 0

-- 1. Check agent table (shows total_requests: 149)
SELECT 
    agent_id,
    name,
    total_requests,
    total_orders,
    total_gmv,
    request_count,
    success_rate,
    last_used_at
FROM agents
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 2. Check if agent_metrics_24h view/table exists and has data
SELECT 
    agent_id,
    requests_24h,
    successful_24h,
    failed_24h,
    success_rate_24h,
    avg_latency_24h,
    gmv_24h,
    orders_24h
FROM agent_metrics_24h
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 3. Check actual usage logs (where the real data is)
SELECT 
    COUNT(*) as total_requests,
    COUNT(CASE WHEN status_code = 200 THEN 1 END) as successful,
    COUNT(CASE WHEN status_code != 200 THEN 1 END) as failed,
    COALESCE(AVG(response_time_ms), 0) as avg_latency,
    COUNT(DISTINCT order_id) FILTER (WHERE order_id IS NOT NULL) as total_orders,
    COALESCE(SUM(order_amount), 0) as total_gmv,
    MIN(timestamp) as first_request,
    MAX(timestamp) as last_request
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 4. Check 24-hour usage (what the API is trying to show)
SELECT 
    COUNT(*) as requests_24h,
    COUNT(CASE WHEN status_code = 200 THEN 1 END) as successful_24h,
    COUNT(CASE WHEN status_code != 200 THEN 1 END) as failed_24h,
    COALESCE(AVG(response_time_ms), 0) as avg_latency_24h,
    COUNT(DISTINCT order_id) FILTER (WHERE order_id IS NOT NULL) as orders_24h,
    COALESCE(SUM(order_amount), 0) as gmv_24h
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
    AND timestamp >= NOW() - INTERVAL '24 hours';

-- 5. Check if agent_policies exists
SELECT * FROM agent_policies
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 6. List all tables that might contain agent metrics
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
AND (table_name LIKE '%agent%' OR table_name LIKE '%metric%')
ORDER BY table_name;
