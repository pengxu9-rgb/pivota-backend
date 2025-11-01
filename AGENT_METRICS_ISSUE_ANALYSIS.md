# Agent Metrics Issue Analysis & Fix

## 问题描述
API 返回的 agent 数据显示为 0，但实际上 agent 有使用记录（149 个请求，5 个订单，$24.99 GMV）

## 根本原因

### 1. 数据库字段混乱
Agents 表有两套字段存储相似数据：

| 字段名 | 值 | 用途 |
|--------|-----|------|
| `request_count` | 0 | 应该存 24h 请求数，但没更新 |
| `total_requests` | 149 | 存储总请求数（正确） |
| `success_rate` | 0 | 应该存成功率，但没更新 |
| `total_orders` | 5 | 存储总订单数（正确） |
| `total_gmv` | 24.99 | 存储总 GMV（正确） |

### 2. Metrics 视图缺失
- 后端代码查询 `agent_metrics_24h` 视图来获取 24 小时指标
- 但这个视图不存在或为空
- 导致 `metrics` 对象返回空 `{}`

### 3. API 字段不一致
- `/employee/agents` 列表端点返回 `request_count`（值为 0）
- `/employee/agents/{id}/details` 详情端点返回：
  - `request_count: 0` 
  - `total_requests: 149`（实际值）

## 修复方案

### 后端修复（已部署）
1. **创建 metrics 视图**
   ```sql
   CREATE VIEW agent_metrics_24h AS
   SELECT ... FROM agent_usage_logs
   WHERE timestamp >= NOW() - INTERVAL '24 hours'
   ```

2. **同步字段数据**
   ```sql
   UPDATE agents SET request_count = total_requests
   ```

3. **新增 API 端点**
   - `GET /admin/fix/agent-metrics-status` - 检查状态
   - `POST /admin/fix/agent-metrics` - 执行修复

### 前端兼容（已部署）
添加字段回退逻辑：
```javascript
requests_24h: agent.request_count || agent.total_requests || 0
```

## 执行修复

### 1. 等待后端部署（2-3 分钟）
Railway 会自动部署新代码

### 2. 运行修复脚本
```bash
./test_fix_agent_metrics.sh YOUR_ADMIN_TOKEN
```

### 3. 验证结果
刷新 Employee Portal，应该看到：
- 7 Day Requests: 149（或实际 7 天内的请求数）
- 7 Day GMV: $24.99（或实际 7 天内的 GMV）
- Success Rate: 实际成功率

## 数据流程图

```
agent_usage_logs (实际请求日志)
    ↓
agent_metrics_24h (24小时统计视图)
    ↓
/employee/agents API
    ↓
Frontend Display
```

## 长期建议

1. **统一字段命名**
   - 删除 `request_count`，只用 `total_requests`
   - 或让 `request_count` 专门存 24h 数据

2. **定期更新统计**
   - 创建定时任务更新 agents 表的统计字段
   - 或完全依赖实时计算的视图

3. **API 响应一致性**
   - 所有端点返回相同的字段结构
   - 避免 flat structure 和 nested structure 混用

## 状态
- ✅ 后端修复已部署
- ✅ 前端兼容已部署
- ⏳ 等待执行数据修复脚本
