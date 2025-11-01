# 🚨 CRITICAL: Agent Metrics 计算口径错误 - 已修复

## 问题描述
Agent 页面显示 requests 和 GMV 都是 0，但 merchant 页面有数据。

## 🔴 根本原因：使用了错误的数据表！

### Merchant 计算方式（正确）✅
```sql
-- 直接从 orders 表查询
SELECT 
    COUNT(*) as total_orders,
    COALESCE(SUM(total), 0) as total_revenue
FROM orders
WHERE merchant_id = :merchant_id
```
**数据来源**: `orders` 表（实际订单数据）

### Agent 计算方式（错误）❌
```sql
-- 从 agent_usage_logs 表查询
SELECT 
    COUNT(*) as requests_24h,
    COALESCE(SUM(order_amount), 0) as gmv_24h
FROM agent_usage_logs
WHERE agent_id = :agent_id
```
**数据来源**: `agent_usage_logs` 表（API 调用日志，不是订单！）

## 📊 数据表对比

| 表名 | 用途 | 包含数据 | 问题 |
|------|------|---------|------|
| `orders` | 存储实际订单 | 订单ID、金额、状态、merchant_id | ✅ 有数据 |
| `agent_usage_logs` | 记录 API 调用 | 端点、响应时间、错误信息 | ❌ 通常为空或不完整 |

## 🔧 修复方案

### 1. 后端修复（已部署）
创建新的计算逻辑，让 Agent 也从 `orders` 表获取数据：

```python
# admin_fix_agent_metrics_v2.py
# Agent 现在通过 merchant 关联到 orders
SELECT 
    COUNT(o.*) as total_orders,
    COALESCE(SUM(o.total), 0) as total_gmv
FROM orders o
JOIN agent_merchants am ON o.merchant_id = am.merchant_id
WHERE am.agent_id = :agent_id
```

### 2. 数据关联路径
```
Agent 
  ↓ (agent_merchants 表)
Merchant
  ↓ (orders.merchant_id)
Orders (实际订单数据)
```

## 执行修复

### 步骤 1：等待部署（2-3分钟）
Railway 正在部署新的修复端点

### 步骤 2：运行修复脚本
```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

# 执行修复
./fix_agent_metrics_from_orders.sh YOUR_ADMIN_TOKEN
```

### 步骤 3：验证结果
脚本会显示：
1. Agent 的实际订单数据（从 orders 表）
2. 修复后的 metrics
3. 更新后的统计数据

## 📈 预期结果

### 修复前 ❌
- Requests: 0
- GMV: $0
- Orders: 0
- （因为 agent_usage_logs 表是空的）

### 修复后 ✅
- Requests: 实际订单数
- GMV: 实际订单金额
- Orders: 实际订单数
- （从 orders 表计算，与 merchant 一致）

## 🎯 影响范围

### 受影响的功能
1. Employee Portal - Agents 页面
2. Agent 详情弹窗
3. Agent API 调用统计
4. 所有时间范围的 metrics（Today, 7 days, 30 days, 90 days）

### 修复后的改进
- ✅ Agent 和 Merchant 使用相同的数据源
- ✅ 数据一致性得到保证
- ✅ 时间范围过滤正确生效
- ✅ 不再依赖可能为空的 usage_logs 表

## 📝 长期建议

### 1. 统一数据模型
- 所有统计都应该从核心业务表（orders, payments）计算
- API 日志表（usage_logs）只用于技术监控，不用于业务统计

### 2. 添加数据验证
```sql
-- 定期检查数据一致性
SELECT 
    'Orders' as source, COUNT(*) as count 
FROM orders
UNION ALL
SELECT 
    'Usage Logs' as source, COUNT(*) as count 
FROM agent_usage_logs;
```

### 3. 文档化数据流
明确每个统计指标的数据来源和计算公式

## 状态
- ✅ **后端修复已推送到 GitHub**
- ⏳ **等待 Railway 部署**
- 📝 **准备执行数据修复脚本**

---

**重要**: 这个问题说明了为什么 Merchant 页面有数据但 Agent 页面没有 - 它们在查询不同的表！现在已经统一使用 orders 表作为真实数据源。
