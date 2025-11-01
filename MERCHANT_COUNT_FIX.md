# Merchant Count 显示为 0 的问题修复

## 问题原因

### ❌ 之前的错误计算
```sql
-- 从 agent_merchants 关联表计算
SELECT COUNT(DISTINCT am.merchant_id) as merchant_count
FROM agent_merchants am
WHERE am.agent_id = 'agent_ee38f2b3645a2ec2'
```

**结果**: 0（因为 agent_merchants 表是空的）

### 实际数据情况
```
agent_merchants 表: 0 条记录 ❌
orders 表: 1 个订单，有 merchant_id ✅
```

这说明：
- Agent 创建订单时直接在 orders 表中记录了 agent_id
- 但没有在 agent_merchants 表中创建关联记录
- 所以查询 agent_merchants 表永远返回 0

## ✅ 修复方案

### 改为从 orders 表直接计算
```sql
-- 从 orders 表计算唯一商户数
SELECT COUNT(DISTINCT o.merchant_id) as merchant_count
FROM orders o
WHERE o.agent_id = 'agent_ee38f2b3645a2ec2'
  AND o.merchant_id IS NOT NULL
```

**结果**: ≥ 1（实际的商户数量）

## 修复的文件

### 1. `employee_agent_mgmt.py` (列表端点)
```python
# Before
LEFT JOIN agent_merchants am ON a.agent_id = am.agent_id

# After  
LEFT JOIN orders o ON a.agent_id = o.agent_id
COUNT(DISTINCT o.merchant_id) FILTER (WHERE o.merchant_id IS NOT NULL)
```

### 2. `employee_agents_management.py` (详情端点)
```python
# 在子查询中添加
COUNT(DISTINCT merchant_id) as merchant_count
FROM orders
WHERE agent_id IS NOT NULL
```

## 数据流程图

### 之前（错误）❌
```
Agent
  ↓ (查询 agent_merchants 表)
agent_merchants 表 (空的)
  ↓
merchant_count = 0
```

### 现在（正确）✅
```
Agent
  ↓ (查询 orders 表)
orders 表 (有订单数据)
  ↓ (COUNT DISTINCT merchant_id)
merchant_count = 实际商户数
```

## 部署和验证

### 1. 等待部署（2-3 分钟）
Railway 正在自动部署修复

### 2. 验证命令
```bash
./verify_deployment.sh YOUR_TOKEN
```

### 3. 预期结果
- **之前**: merchant_count: 0
- **之后**: merchant_count: 1（或实际的唯一商户数）

### 4. 前端显示
刷新 Employee Portal 后，应该看到：
- Merchants 列：显示实际数量（≥ 1）而不是 0

## 为什么 agent_merchants 表是空的？

可能的原因：
1. **订单创建流程**直接设置了 agent_id，没有同步更新关联表
2. **历史数据**：关联表功能可能是后来添加的
3. **数据迁移**：可能需要从 orders 表反向填充 agent_merchants 表

## 建议的数据修复（可选）

如果想填充 agent_merchants 表：
```sql
-- 从 orders 表反向创建关联关系
INSERT INTO agent_merchants (agent_id, merchant_id, connected_at)
SELECT DISTINCT 
    agent_id, 
    merchant_id,
    MIN(created_at) as connected_at
FROM orders
WHERE agent_id IS NOT NULL 
  AND merchant_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM agent_merchants am 
    WHERE am.agent_id = orders.agent_id 
      AND am.merchant_id = orders.merchant_id
  )
GROUP BY agent_id, merchant_id;
```

但这不是必需的，因为我们现在直接从 orders 表计算了。

## 总结

✅ **问题**: merchant_count 显示 0
✅ **原因**: 从空的 agent_merchants 表查询
✅ **修复**: 改为从 orders 表计算 COUNT(DISTINCT merchant_id)
⏳ **状态**: 已推送，等待部署

---

**注意**: 这个修复让 merchant_count 反映真实情况 - 这个 agent 实际服务了多少个不同的商户。
