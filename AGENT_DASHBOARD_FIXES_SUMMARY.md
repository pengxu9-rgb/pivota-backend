# Agent Dashboard 数据修复总结

**Date**: 2025-10-27  
**Status**: ✅ 已部署修复

---

## 🎯 已修复的问题

### ✅ 1. **Avg Order Value 计算错误**

**之前的错误**:
```typescript
// ❌ 错误：用 GMV 除以 API 调用数
metrics.total_gmv / metrics.calls_today
// 结果：$8.33 (完全错误)
```

**现在的正确计算**:
```typescript
// ✅ 正确：用总收入除以付款订单数
orderStats.total_revenue / orderStats.paid_orders
// 结果：$99.00 (1个付款订单，总计$99)
```

**后端更新**:
- 添加了全时间段（all-time）的订单统计：
  - `total_orders`: 所有订单数（4个）
  - `total_paid_orders`: 已付款订单数（1个）
  - `total_revenue`: 总收入（$99）

---

### ✅ 2. **Total Integrations (商户数)**

**修复**:
- 后端现在计算这个 agent 关联的**唯一商户数**
- 使用 `SELECT COUNT(DISTINCT merchant_id) FROM orders WHERE agent_id = ?`
- 前端使用 `data.merchants.total_count`

---

## ⏳ 待修复的问题

根据你的反馈，以下指标仍然有问题，需要继续检查和修复：

### 🔴 3. **MCP Query Analysis 全是 0**

**问题**: Product Searches, Inventory Checks, Price Queries 都显示 0

**原因**: `/agent/v1/analytics/queries` 端点从 `agent_usage_logs` 表查询，但可能：
1. 表中没有数据（没有记录 API 调用）
2. `endpoint` 字段的格式不匹配（例如查询 `%/products/search%` 但实际是 `/agent/v1/products/search`）

**需要检查**:
```sql
-- 查看 agent_usage_logs 表中有什么数据
SELECT endpoint, COUNT(*) as count
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY endpoint
ORDER BY count DESC;

-- 检查是否有 product/inventory/price 相关的端点
SELECT DISTINCT endpoint
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
AND (endpoint LIKE '%product%' OR endpoint LIKE '%inventory%' OR endpoint LIKE '%pric%');
```

---

### 🔴 4. **Analytics Performance Timeline (past 24h)**

**问题**: 数据不准确

**可能原因**:
- 前端调用的 API 端点错误
- 后端返回的时间序列数据格式不对
- 数据时区问题

**需要检查**: Analytics 页面的代码和 API 调用

---

### 🔴 5. **Total API Calls**

**问题**: 显示的数字不对

**当前逻辑**:
```sql
SELECT COUNT(*) FROM agent_usage_logs WHERE agent_id = :agent_id
```

**需要验证**:
- 这个数字是否与实际 API 调用数匹配
- 是否应该有时间范围限制（例如只显示最近7天）

---

### 🔴 6. **Usage by Endpoints**

**问题**: 数据可能不准确

**当前逻辑**:
```sql
SELECT endpoint, COUNT(*) as count 
FROM agent_usage_logs 
WHERE agent_id = :agent_id AND timestamp >= :last_24h
GROUP BY endpoint 
ORDER BY count DESC 
LIMIT 5
```

**需要验证**:
- `agent_usage_logs` 表中是否有完整的端点记录
- 端点名称是否正确格式化

---

## 🔧 下一步行动

### 立即验证（部署完成后）:
```bash
# 1. 测试新的 metrics API
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://web-production-fedb.up.railway.app/agent/metrics/summary"

# 应该看到：
# - orders.total_orders: 4
# - orders.total_paid_orders: 1
# - orders.total_revenue: 99
# - merchants.total_count: 1
```

### 2. 检查 agent_usage_logs 表:
```sql
-- 执行以下 SQL 并分享结果
SELECT 
    COUNT(*) as total_logs,
    COUNT(DISTINCT endpoint) as unique_endpoints,
    MIN(timestamp) as earliest,
    MAX(timestamp) as latest
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 查看所有端点
SELECT endpoint, COUNT(*) as count
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY endpoint
ORDER BY count DESC;
```

### 3. 如果 agent_usage_logs 为空:
需要确认：
- Agent API 调用时是否正确记录到 `agent_usage_logs`
- `log_agent_request` 函数是否被正确调用
- 是否有数据库写入错误

---

## 📊 预期结果（修复后）

**Dashboard 顶部指标**:
- ✅ **API Calls Today**: 实际24小时内的调用数
- ✅ **Success Rate**: 基于 status_code < 400 计算
- ✅ **Avg Response Time**: agent_usage_logs 的平均值
- ✅ **Total Integrations**: 1 (chydantest.myshopify.com)

**Orders 卡片**:
- ✅ **Total Orders**: 4
- ✅ **Total GMV**: $99 (或全部订单总额)
- ✅ **Avg Order Value**: $99.00 (99/1)

**MCP Query Analysis**:
- 🔄 **Product Searches**: 待修复（需要确认 agent_usage_logs 数据）
- 🔄 **Inventory Checks**: 待修复
- 🔄 **Price Queries**: 待修复

---

## 🚀 部署状态

- ✅ **Backend**: Railway 部署中（约2-3分钟）
- ✅ **Agent Portal**: Vercel 部署中（约1-2分钟）

**等待部署完成后，请刷新 Agent Dashboard 并告诉我**:
1. Avg Order Value 是否显示 $99.00
2. Total Integrations 是否显示 1
3. 其他指标的具体数值（我们继续修复）

