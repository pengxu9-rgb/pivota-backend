-- 修复部署后的数据库问题

-- 1. 添加缺失的 total_gmv 列到 agents 表
ALTER TABLE agents 
ADD COLUMN IF NOT EXISTS total_gmv DECIMAL(12,2) DEFAULT 0;

-- 2. 修复 agent_usage_logs 表的 request_id 约束问题
-- 先删除有问题的空 request_id 记录
DELETE FROM agent_usage_logs 
WHERE request_id = '' OR request_id IS NULL;

-- 确保 request_id 不能为空
ALTER TABLE agent_usage_logs 
ALTER COLUMN request_id SET DEFAULT gen_random_uuid()::text;

-- 3. 确保 products_cache 表的 expires_at 字段正确设置
ALTER TABLE products_cache 
ALTER COLUMN expires_at SET DEFAULT (NOW() + INTERVAL '24 hours');

-- 更新现有的 NULL expires_at 值
UPDATE products_cache 
SET expires_at = cached_at + INTERVAL '24 hours'
WHERE expires_at IS NULL;

-- 4. 验证修复
SELECT 
    'agents.total_gmv column' as check_item,
    CASE 
        WHEN column_name IS NOT NULL THEN 'OK'
        ELSE 'MISSING'
    END as status
FROM information_schema.columns
WHERE table_name = 'agents' AND column_name = 'total_gmv'
UNION ALL
SELECT 
    'agent_usage_logs empty request_ids' as check_item,
    CASE 
        WHEN COUNT(*) = 0 THEN 'OK'
        ELSE 'STILL EXISTS: ' || COUNT(*)
    END as status
FROM agent_usage_logs
WHERE request_id = '' OR request_id IS NULL
UNION ALL
SELECT 
    'products_cache null expires_at' as check_item,
    CASE 
        WHEN COUNT(*) = 0 THEN 'OK'
        ELSE 'STILL EXISTS: ' || COUNT(*)
    END as status
FROM products_cache
WHERE expires_at IS NULL;




