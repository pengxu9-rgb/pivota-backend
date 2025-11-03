# Railway PostgreSQL 诊断查询

## 🔍 请在 Railway > PostgreSQL > Query 中运行以下查询

### 查询 1: 查看所有 PSP 配置

```sql
SELECT 
    psp_id,
    merchant_id,
    provider,
    name,
    LENGTH(api_key) as api_key_len,
    SUBSTRING(api_key, 1, 10) as api_key_prefix,
    account_id,
    CASE WHEN secret_key IS NOT NULL THEN LENGTH(secret_key) ELSE 0 END as secret_len,
    status,
    connected_at
FROM merchant_psps 
ORDER BY connected_at DESC 
LIMIT 20;
```

**请复制结果给我！**

---

### 查询 2: 查看特定商户的所有 PSP

```sql
SELECT 
    psp_id,
    provider,
    LENGTH(api_key) as api_key_len,
    account_id,
    CASE WHEN secret_key IS NOT NULL THEN 'YES' ELSE 'NO' END as has_secret,
    status,
    connected_at
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;
```

**请复制结果给我！**

---

### 查询 3: 统计各 PSP 的数量

```sql
SELECT 
    provider,
    COUNT(*) as count,
    COUNT(DISTINCT merchant_id) as unique_merchants
FROM merchant_psps 
GROUP BY provider
ORDER BY count DESC;
```

---

### 查询 4: 检查是否有重复的 psp_id

```sql
SELECT psp_id, COUNT(*) as count
FROM merchant_psps 
GROUP BY psp_id
HAVING COUNT(*) > 1;
```

如果这个查询有结果，说明有重复的 psp_id（这会导致 UPSERT 问题）

---

### 查询 5: 删除失败的配置（如果需要）

⚠️ **只在确认有重复或错误记录时运行！**

```sql
-- 删除特定商户的特定 PSP
DELETE FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42' 
  AND provider = 'adyen';

-- 或者删除 status != 'active' 的记录
DELETE FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42' 
  AND status != 'active';
```

---

## 🎯 期望的结果

如果一切正常，查询 2 应该显示：

```
psp_id              | provider | api_key_len | account_id | has_secret | status | connected_at
--------------------|----------|-------------|------------|------------|--------|-------------
psp_checkout_...    | checkout | 35          | pc_...     | NO         | active | 2025-10-26...
psp_paypal_...      | paypal   | 80          | NULL       | YES        | active | 2025-10-26...
psp_adyen_...       | adyen    | 25          | TestMer... | NO         | active | 2025-10-26...
```

如果没有 adyen 和 paypal 的记录，说明配置确实没有保存。

---

## 🐛 可能的问题

### 如果看到多条相同 provider 的记录：
- 可能是每次配置都生成了新的 psp_id
- ON CONFLICT 没有触发，导致插入了新记录
- 但由于某些原因这些记录是 inactive 或有其他问题

### 如果完全没有记录：
- INSERT 失败（可能是约束冲突）
- 事务被回滚
- 权限问题

### 如果 api_key_len 是 0 或 NULL：
- API key 没有正确传递
- 或者被覆盖为空

---

请运行查询 1 和 2，并把结果告诉我！


