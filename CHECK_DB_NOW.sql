-- 立即在 Railway PostgreSQL 中运行这些查询
-- 复制结果给我

-- 查询 1: 查看特定商户的所有 PSP（包括 inactive）
SELECT 
    psp_id,
    provider,
    LENGTH(api_key) as api_key_len,
    SUBSTRING(api_key, 1, 15) as api_key_prefix,
    account_id,
    CASE WHEN secret_key IS NOT NULL THEN LENGTH(secret_key) ELSE 0 END as secret_len,
    status,
    connected_at
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;

-- 查询 2: 查看最近的所有 PSP 保存（所有商户）
SELECT 
    psp_id,
    merchant_id,
    provider,
    LENGTH(api_key) as api_key_len,
    status,
    connected_at
FROM merchant_psps 
ORDER BY connected_at DESC 
LIMIT 10;

-- 查询 3: 统计各 provider 的数量
SELECT 
    provider,
    status,
    COUNT(*) as count
FROM merchant_psps 
GROUP BY provider, status
ORDER BY provider, status;


