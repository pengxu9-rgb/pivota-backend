# Merchant Count 修复完成

## ✅ 已修复的代码（Commit: c9643b83）

### SQL 查询更新

```sql
-- 之前（简化版，merchant_count = 0）
SELECT * FROM agents

-- 现在（带 merchant_count 计算）
SELECT 
    a.*,
    COUNT(DISTINCT o.merchant_id) as merchant_count
FROM agents a
LEFT JOIN orders o ON a.agent_id = o.agent_id 
    AND o.merchant_id IS NOT NULL
GROUP BY a.agent_id
```

### 计算逻辑

- **数据源**: `orders` 表（不是 `agent_merchants` 表）
- **计算**: `COUNT(DISTINCT merchant_id)` - 去重统计
- **含义**: 这个 agent 服务了多少个不同的商户

## 部署与验证

### 1. Railway Redeploy（Commit: c9643b83）

### 2. 部署完成后测试：

```bash
curl -sS 'https://web-production-fedb.up.railway.app/employee/agents?date_range=7d' \
  -H 'Authorization: Bearer YOUR_TOKEN' | python3 -m json.tool
```

**预期结果**：
```json
{
  "agents": [{
    "merchant_count": 1  // ✅ 不再是 0
  }]
}
```

### 3. 刷新 Employee Portal

**应该看到**：
- Merchants 列：显示 **1**（或实际的唯一商户数）

## 为什么之前是 0？

### 数据关系

```
Agent: agent_ee38f2b3645a2ec2
  ↓ (orders.agent_id)
Orders: 1 个订单
  ↓ (orders.merchant_id)  
Merchant: 1 个商户
```

### 之前的计算（错误）
```sql
-- 从 agent_merchants 关联表
COUNT(DISTINCT am.merchant_id) FROM agent_merchants
→ 结果: 0（表是空的）
```

### 现在的计算（正确）
```sql
-- 从 orders 表直接统计
COUNT(DISTINCT o.merchant_id) FROM orders WHERE agent_id = ?
→ 结果: 1（实际商户数）
```

## 最终数据预览

部署后，列表应该显示：

| 字段 | 值 | 说明 |
|------|-----|------|
| agent_name | asdf | ✅ 正确 |
| total_orders | 1 | ✅ 从 agents 表 |
| total_gmv | 24.99 | ✅ 从 agents 表 |
| merchant_count | 1 | ✅ 从 orders 表计算 |
| request_count | 1 | ✅ 正确 |
| success_rate | 100.0 | ✅ 正确 |

---

**请 Railway Redeploy 后告诉我结果！**

