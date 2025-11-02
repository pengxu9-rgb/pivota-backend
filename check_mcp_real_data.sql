-- 检查 MCP 相关的真实数据

-- 1. 检查 merchant_store_integrations 表
SELECT 
    'merchant_store_integrations' as table_name,
    COUNT(*) as total_records,
    COUNT(DISTINCT merchant_id) as unique_merchants
FROM merchant_store_integrations;

-- 2. 查看具体的 store 数据
SELECT 
    integration_id,
    merchant_id,
    platform,
    store_name,
    status,
    created_at
FROM merchant_store_integrations
ORDER BY created_at DESC
LIMIT 10;

-- 3. 检查是否有产品数据
SELECT 
    m.merchant_id,
    m.business_name,
    COUNT(DISTINCT msi.integration_id) as store_count,
    COUNT(DISTINCT pc.product_id) as product_count
FROM merchant_onboarding m
LEFT JOIN merchant_store_integrations msi ON m.merchant_id = msi.merchant_id
LEFT JOIN products_cache pc ON m.merchant_id = pc.merchant_id
GROUP BY m.merchant_id, m.business_name
LIMIT 10;

-- 4. 检查 PSP 连接
SELECT 
    merchant_id,
    COUNT(*) as psp_count,
    STRING_AGG(provider, ', ') as providers
FROM merchant_psps
WHERE status = 'active'
GROUP BY merchant_id;

-- 5. 检查是否数据在其他表（旧的 MCP 字段）
SELECT 
    merchant_id,
    business_name,
    mcp_platform,
    mcp_shop_domain,
    mcp_connected
FROM merchant_onboarding
WHERE mcp_connected = true OR mcp_platform IS NOT NULL
LIMIT 10;

