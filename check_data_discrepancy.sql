-- 检查数据计算口径差异

-- 1. Merchant 数据来源：直接从 orders 表
-- merchant_api_extensions.py 第 46-58 行
SELECT 
    'Merchant Data' as source,
    COUNT(*) as total_orders,
    COALESCE(SUM(total), 0) as total_revenue
FROM orders
WHERE merchant_id IN (SELECT DISTINCT merchant_id FROM orders);

-- 2. Agent 数据：从 agent_usage_logs 表
-- 但 agent_usage_logs 是 API 调用日志，不是订单数据！
SELECT 
    'Agent Usage Logs' as source,
    COUNT(*) as total_logs,
    COUNT(DISTINCT order_id) as unique_orders,
    COALESCE(SUM(order_amount), 0) as total_gmv
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 3. 问题核心：Agent 应该从 orders 表统计，不是从 usage_logs！
-- 正确的 Agent 统计应该是：
SELECT 
    'Agent Orders (Correct)' as source,
    COUNT(*) as total_orders,
    COALESCE(SUM(total), 0) as total_gmv,
    COUNT(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 END) as paid_orders
FROM orders  
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 4. 检查 orders 表是否有 agent_id 字段
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'orders' 
AND column_name LIKE '%agent%';

-- 5. 如果没有 agent_id，需要通过其他方式关联
-- 可能通过 merchant_id -> agent_merchants -> agent_id
SELECT 
    'Agent via Merchant' as source,
    a.agent_id,
    COUNT(o.*) as total_orders,
    COALESCE(SUM(o.total), 0) as total_gmv
FROM agents a
JOIN agent_merchants am ON a.agent_id = am.agent_id  
JOIN orders o ON am.merchant_id = o.merchant_id
WHERE a.agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY a.agent_id;
