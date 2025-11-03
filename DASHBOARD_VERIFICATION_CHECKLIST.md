# Agent Dashboard 全面验证清单

## 🔧 已修复的问题

### 1. ✅ Total Integrations
- **之前**: 显示 active agents 数量（不对）
- **现在**: 显示该 agent 授权的商家数量
- **数据源**: GET `/agents/{agent_id}/merchants`

### 2. ✅ Active Now
- **含义**: 最近 1 小时的 API 调用次数
- **数据源**: `summary.overview.requests_last_hour`

### 3. ✅ API Calls Today
- **含义**: 最近 24 小时的 API 调用次数
- **数据源**: `summary.overview.requests_last_24h`

### 4. ✅ Avg Response Time
- **含义**: 平均响应时间（毫秒）
- **数据源**: `summary.performance.avg_response_time_ms`

### 5. ✅ MCP Query Analytics
- **数据源**: GET `/agents/{agent_id}/query-analytics`
- **字段映射**:
  - product_searches + trend + change
  - inventory_checks + trend
  - price_queries + trend
- **统计来源**: `agent_usage_logs` 表，匹配 endpoint 模式

### 6. ✅ Order Conversion Funnel
- **数据源**: GET `/agents/{agent_id}/funnel`
- **字段**: orders_initiated, payment_attempted, orders_completed
- **统计来源**: `orders` 表，按 `agent_id` 过滤

### 7. ✅ Recent API Activity
- **数据源**: GET `/agent/v1/metrics/recent?limit=5&agent_id={agent_id}`
- **显示**: 最近 5 条，点击 "More" 加载更多
- **统计来源**: `agent_usage_logs` 表

### 8. ✅ Total GMV Today
- **数据源**: `summary.orders.revenue_last_24h`
- **统计来源**: `orders` 表，SUM(total)，最近 24 小时

---

## 🐛 当前问题和原因

### Problem: Order Funnel 返回 mock 数据 (89/82/76)

**原因**: 
- 查询 `orders.agent_id = 'agent@test.com'`
- 但创建订单时 `agent_id` 字段可能为 NULL

**验证命令**:
```bash
curl -s "https://web-production-fedb.up.railway.app/agents/agent@test.com/funnel?days=7" \
  -H "Authorization: Bearer YOUR_TOKEN" | python3 -m json.tool
```

**如果返回 mock 数据**，说明查询出错，走到了 except 分支。

**解决方案**: 
1. 检查 `orders` 表中是否有 `agent_id` 字段
2. 确保创建订单时正确填充 `agent_id`

---

## 📊 完整验证步骤

### 部署完成后（约 3 分钟）

#### 1. 重新登录
- 访问 https://agents.pivota.cc
- Logout → 重新登录 agent@test.com
- 检查浏览器控制台是否显示 "✅ Agent API key auto-saved"

#### 2. 验证 localStorage
在浏览器控制台：
```javascript
console.log('Token:', !!localStorage.agent_token);
console.log('Agent ID:', localStorage.agent_id);
console.log('API Key:', localStorage.agent_api_key?.substring(0, 20) + '...');
```

#### 3. 生成测试数据
```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344
python3 generate_orders_with_progress.py
```

#### 4. 检查每个指标

##### Dashboard 页面
- [ ] **Total Integrations**: 应该显示商家数（如 1 或更多）
- [ ] **Active Now**: 显示最近 1 小时的调用数
- [ ] **API Calls Today**: 显示最近 24 小时的调用数（应该 > 0）
- [ ] **Avg Response Time**: 显示真实的响应时间（如 1631ms）
- [ ] **MCP Query Analytics**: 
  - Product Searches: 应该 > 0
  - Inventory Checks: 可能为 0
  - Price Queries: 可能为 0
- [ ] **Order Conversion Funnel**:
  - Orders Initiated: 应该 > 0
  - Payment Attempted: 应该 > 0
  - Orders Completed: 应该 > 0
- [ ] **Recent API Activity**: 显示最近 5 条真实调用
- [ ] **Total GMV Today**: 显示真实金额（如 $12,544.98）
- [ ] **Success Rate**: 应该是真实百分比（如 100%）

##### Analytics 页面
- [ ] **Total API Calls**: 显示真实数字
- [ ] **Total Orders**: 显示真实订单数
- [ ] **Total GMV**: 显示真实金额
- [ ] **Success Rate**: 显示真实百分比
- [ ] **Performance Timeline**: 显示按小时分组的数据
- [ ] **API Usage by Endpoint**: 显示各端点调用次数

---

## 🔍 调试命令

### 检查后端数据
```bash
# Summary
curl https://web-production-fedb.up.railway.app/agent/v1/metrics/summary | python3 -m json.tool

# Recent Activity
curl "https://web-production-fedb.up.railway.app/agent/v1/metrics/recent?limit=5&agent_id=agent@test.com" | python3 -m json.tool

# Funnel (需要 token)
curl "https://web-production-fedb.up.railway.app/agents/agent@test.com/funnel" \
  -H "Authorization: Bearer YOUR_TOKEN" | python3 -m json.tool

# Query Analytics (需要 token)
curl "https://web-production-fedb.up.railway.app/agents/agent@test.com/query-analytics" \
  -H "Authorization: Bearer YOUR_TOKEN" | python3 -m json.tool

# Usage logs summary
curl https://web-production-fedb.up.railway.app/admin/debug/usage-logs/summary | python3 -m json.tool
```

### 检查 orders 表中的 agent_id
需要直接查询数据库或创建一个调试端点：
```sql
SELECT 
    COUNT(*) as total,
    COUNT(agent_id) as with_agent_id,
    COUNT(*) - COUNT(agent_id) as missing_agent_id
FROM orders
WHERE created_at >= NOW() - INTERVAL '7 days';
```

---

## 🎯 下一步行动

1. **等待部署完成**（后端 + 前端，约 3-5 分钟）
2. **重新登录** Agent Portal（自动获取 API key）
3. **刷新 Dashboard** 查看所有指标
4. **如果 Funnel 仍然错误**:
   - 我会创建调试端点检查 `orders.agent_id` 字段
   - 修复订单创建逻辑以正确填充 `agent_id`
5. **全面扫描所有页面** 确保没有其他问题

---

## 📝 预期的正确数据（基于当前后端）

根据 `/admin/debug/usage-logs/summary`:
- **API Calls Today**: 244
- **Orders Created**: 243
- **Product Searches**: 1
- **Total GMV**: $12,544.98
- **Success Rate**: 100%

如果 Dashboard 显示的数字接近这些，说明映射正确！





