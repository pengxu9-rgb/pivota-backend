-- 调试产品缓存问题

-- 1. 检查products_cache表结构
SELECT column_name, data_type, is_nullable 
FROM information_schema.columns 
WHERE table_name = 'products_cache'
ORDER BY ordinal_position;

-- 2. 检查是否有数据写入
SELECT 
    merchant_id,
    platform,
    COUNT(*) as product_count,
    MAX(cached_at) as last_cached,
    MIN(expires_at) as first_expires
FROM products_cache
GROUP BY merchant_id, platform
ORDER BY last_cached DESC;

-- 3. 检查具体产品数据
SELECT 
    id,
    merchant_id,
    platform,
    platform_product_id,
    LENGTH(product_data::text) as data_size,
    cache_status,
    cached_at,
    expires_at,
    ttl_seconds
FROM products_cache
ORDER BY cached_at DESC
LIMIT 10;

-- 4. 检查是否有过期时间问题
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN expires_at < NOW() THEN 1 ELSE 0 END) as expired,
    SUM(CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END) as null_expires
FROM products_cache;

-- 5. 检查v2端点是否正确过滤过期产品
SELECT 
    merchant_id,
    platform,
    COUNT(*) as total_products,
    SUM(CASE WHEN (cached_at + INTERVAL '1 second' * ttl_seconds) > NOW() THEN 1 ELSE 0 END) as valid_products
FROM products_cache
GROUP BY merchant_id, platform;




