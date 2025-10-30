
-- 数据迁移脚本：将 mcp_* 数据迁移到 merchant_stores

-- 1. 备份现有数据
CREATE TABLE merchant_onboarding_backup AS SELECT * FROM merchant_onboarding;
CREATE TABLE merchant_stores_backup AS SELECT * FROM merchant_stores;

-- 2. 迁移数据到 merchant_stores
INSERT INTO merchant_stores (
    store_id,
    merchant_id,
    platform,
    name,
    domain,
    api_key,
    status,
    connected_at
)
SELECT 
    'legacy_' || merchant_id || '_' || mcp_platform as store_id,
    merchant_id,
    mcp_platform as platform,
    business_name as name,
    mcp_shop_domain as domain,
    mcp_access_token as api_key,
    'active' as status,
    COALESCE(mcp_connected_at, NOW()) as connected_at
FROM merchant_onboarding
WHERE mcp_connected = true
AND merchant_id NOT IN (
    SELECT DISTINCT merchant_id FROM merchant_stores
);

-- 3. 验证迁移
SELECT 
    'Original MCP merchants:' as description,
    COUNT(*) as count
FROM merchant_onboarding 
WHERE mcp_connected = true
UNION ALL
SELECT 
    'Migrated to merchant_stores:' as description,
    COUNT(DISTINCT merchant_id) as count
FROM merchant_stores;

-- 4. 清理（在确认无误后执行）
-- UPDATE merchant_onboarding SET mcp_connected = null, mcp_platform = null, 
--        mcp_shop_domain = null, mcp_access_token = null;
