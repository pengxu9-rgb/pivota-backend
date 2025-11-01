-- Check if products were actually cached
SELECT 
    merchant_id,
    platform,
    COUNT(*) as product_count,
    MAX(cached_at) as last_cached
FROM products_cache
WHERE merchant_id IN (
    SELECT DISTINCT merchant_id 
    FROM merchant_stores 
    WHERE platform = 'wix'
)
GROUP BY merchant_id, platform
ORDER BY last_cached DESC;

-- Check specific merchant's products
SELECT 
    merchant_id,
    platform,
    platform_product_id,
    json_extract(product_data, '$.title') as product_title,
    json_extract(product_data, '$.price') as price,
    cached_at
FROM products_cache
WHERE platform = 'wix'
ORDER BY cached_at DESC
LIMIT 10;

-- Check if there's a mismatch between merchant_stores and merchant_onboarding
SELECT 
    ms.merchant_id,
    ms.platform as store_platform,
    ms.status as store_status,
    mo.mcp_connected,
    mo.mcp_platform
FROM merchant_stores ms
LEFT JOIN merchant_onboarding mo ON ms.merchant_id = mo.merchant_id
WHERE ms.platform = 'wix';




