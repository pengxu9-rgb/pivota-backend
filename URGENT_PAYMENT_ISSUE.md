# 🚨 紧急：所有 PSP 支付意图创建失败

## 问题现象

**所有 PSP（Stripe, Adyen, Checkout, PayPal）都返回 null 的 `client_secret` 和 `payment_intent_id`**

### 测试结果：

```json
{
    "status": "success",
    "order_id": "ORD_xxx",
    "payment": {
        "client_secret": null,
        "payment_intent_id": null
    }
}
```

这意味着：
- ✅ 订单创建成功
- ❌ **支付意图创建完全失败**
- ❌ 用户无法进行支付

## 可能的原因

1. **PSP 配置未保存到数据库**
   - 虽然 Employee Portal 显示配置成功
   - 但数据可能没有真正持久化

2. **order_routes.py 逻辑问题**
   - 可能没有正确查询 PSP 配置
   - 或者查询后没有正确使用

3. **数据库查询失败**
   - `status = 'active'` 条件太严格
   - 或者数据库中根本没有记录

## 立即检查

### 1. 检查 Railway 数据库

在 Railway Dashboard 中运行：

```sql
-- 查看所有 PSP 配置
SELECT 
    psp_id,
    merchant_id,
    provider,
    LENGTH(api_key) as key_length,
    SUBSTRING(api_key, 1, 10) as key_prefix,
    account_id,
    CASE WHEN secret_key IS NOT NULL THEN 'YES' ELSE 'NO' END as has_secret,
    status,
    connected_at
FROM merchant_psps
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;

-- 检查是否所有 PSP 都是 active
SELECT provider, status, COUNT(*) 
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
GROUP BY provider, status;
```

### 2. 检查 Railway 日志

查找最近的订单创建日志，关注：
- `✅ Found {psp_type} key in DB` - 是否找到了 PSP 配置？
- `📡 Creating {psp_type} payment intent` - 是否尝试创建支付？
- 任何错误信息

关键日志搜索词：
- "merch_208139f7600dbf42"
- "No {psp} config in DB"
- "Payment intent creation failed"
- "ORD_813187750E174922" (Adyen 测试订单)
- "ORD_D654C889B750E3B6" (PayPal 测试订单)

### 3. 可能的修复

#### 修复 A: PSP 状态问题

如果数据库中 PSP 的 status 不是 'active'：

```sql
-- 更新所有 PSP 为 active
UPDATE merchant_psps
SET status = 'active'
WHERE merchant_id = 'merch_208139f7600dbf42';
```

#### 修复 B: 重新插入 PSP

如果数据库中没有记录，使用调试端点：

```bash
# 使用 debug PSP insert 端点
curl -X POST https://web-production-fedb.up.railway.app/debug/insert-psp \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "provider": "adyen",
    "api_key": "YOUR_ADYEN_KEY",
    "account_id": "YOUR_MERCHANT_ACCOUNT"
  }'
```

#### 修复 C: 修改代码逻辑

如果问题出在查询逻辑，需要修改 `order_routes.py` 第 267 行：

```python
# 移除 status = 'active' 限制
psp_row = await database.fetch_one(
    """
    SELECT api_key, account_id, secret_key FROM merchant_psps
    WHERE merchant_id = :merchant_id AND provider = :provider
    ORDER BY connected_at DESC
    LIMIT 1
    """,
    {"merchant_id": order_request.merchant_id, "provider": psp_type}
)
```

## 下一步行动

请立即：

1. **查看 Railway 数据库** - 运行上面的 SQL 查询
2. **查看 Railway 日志** - 搜索订单 ID 和相关错误
3. **将结果发给我** - 我会根据实际情况给出精确修复方案

## 临时解决方案

如果需要立即测试 Stripe（通常配置正确的那个）：

```bash
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "stripe",
    ...
  }'
```

如果连 Stripe 也失败，说明是系统性问题，可能是：
- 数据库连接问题
- PSP adapter 初始化失败
- 配置完全没有保存

