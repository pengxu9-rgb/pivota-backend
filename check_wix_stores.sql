-- Check Wix stores in database
SELECT 
    ms.store_id,
    ms.merchant_id,
    ms.platform,
    ms.name,
    ms.domain,
    CASE WHEN ms.api_key IS NOT NULL THEN 'Has API Key' ELSE 'No API Key' END as api_key_status,
    ms.status,
    ms.connected_at,
    ms.last_sync,
    ms.product_count,
    mo.business_name,
    mo.user_id
FROM merchant_stores ms
LEFT JOIN merchant_onboarding mo ON ms.merchant_id = mo.merchant_id
WHERE ms.platform = 'wix'
ORDER BY ms.connected_at DESC;

-- Check if there are any active Wix stores
SELECT COUNT(*) as total_wix_stores,
       SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_stores,
       SUM(CASE WHEN api_key IS NOT NULL THEN 1 ELSE 0 END) as stores_with_api_key
FROM merchant_stores
WHERE platform = 'wix';

-- Check latest product sync attempts
SELECT 
    merchant_id,
    platform,
    COUNT(*) as products_in_cache,
    MAX(cached_at) as last_cached
FROM products_cache
WHERE platform = 'wix'
GROUP BY merchant_id, platform
ORDER BY last_cached DESC
LIMIT 10;




