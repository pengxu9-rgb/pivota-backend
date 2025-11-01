# Metrics Interrelation Analysis - Portal间的数据关系

## 📊 系统架构概览

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Agent Portal   │     │ Merchant Portal │     │ Employee Portal │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                         │
         └───────────────────────┼─────────────────────────┘
                                 │
                         ┌───────▼────────┐
                         │  Backend API   │
                         │ (Railway Host) │
                         └───────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
              ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
              │  Orders   │ │ Agent  │ │ Merchant │
              │  Table    │ │ Usage  │ │  Stores  │
              └───────────┘ │ Logs   │ └──────────┘
                           └────────┘
```

## 🔗 数据流关系

### 1. Agent Portal Metrics

**主要数据源**:
- `agent_usage_logs` - API调用记录
- `orders` - 订单数据
- `agent_merchants` - 商家授权关系

**关键指标**:
```javascript
// GET /agent/v1/metrics/summary
{
  "overview": {
    "total_requests": 12345,        // 来自 agent_usage_logs
    "requests_last_hour": 45,       // 最近1小时API调用
    "requests_last_24h": 244        // 最近24小时API调用
  },
  "performance": {
    "success_rate_24h": 98.5,       // status_code < 400 的比例
    "avg_response_time_ms": 145.2  // 平均响应时间
  },
  "orders": {
    "count_last_24h": 20,           // 24小时订单数
    "revenue_last_24h": 12545.00,   // 24小时GMV
    "total_revenue": 156789.00      // 总GMV
  }
}
```

### 2. Merchant Portal Metrics

**主要数据源**:
- `orders` - 该商家的订单
- `merchant_stores` - 商店连接状态
- `products_cache` - 产品数据

**关键指标**:
```javascript
// GET /merchant/dashboard/stats
{
  "revenue": {
    "today": 1234.56,               // 今日收入
    "month": 45678.90,              // 本月收入
    "total": 234567.89              // 总收入
  },
  "orders": {
    "pending": 5,                   // 待处理订单
    "completed": 145,               // 已完成订单
    "total": 156                    // 总订单数
  },
  "products": {
    "total": 234,                   // 产品总数
    "out_of_stock": 12              // 缺货产品
  }
}
```

### 3. Employee Portal Metrics

**主要数据源**:
- 汇总所有Agent和Merchant数据
- `agent_usage_logs` - 所有API调用
- `orders` - 所有订单
- `merchant_onboarding` - 商家状态

**关键指标**:
```javascript
// GET /analytics/dashboard
{
  "platform_overview": {
    "total_gmv": 1234567.89,        // 平台总GMV
    "active_merchants": 145,         // 活跃商家数
    "active_agents": 12,            // 活跃Agent数
    "total_orders": 45678           // 总订单数
  },
  "performance": {
    "system_uptime": 99.9,          // 系统正常运行时间
    "api_success_rate": 98.5,       // API成功率
    "avg_response_time": 145        // 平均响应时间
  }
}
```

## 📈 数据关联点

### 1. 订单数据流
```
Agent创建订单 → orders表(agent_id) → Merchant看到订单 → Employee监控全局
```

### 2. API使用追踪
```
Agent调用API → agent_usage_logs → Agent Portal显示 → Employee Portal汇总
```

### 3. 商家授权关系
```
Merchant授权Agent → agent_merchants表 → Agent看到可访问商家 → Employee管理权限
```

## 🔍 关键数据关系

### orders表是核心
```sql
-- Agent视角：只看自己创建的订单
SELECT * FROM orders WHERE agent_id = 'agent_123'

-- Merchant视角：只看自己店铺的订单  
SELECT * FROM orders WHERE merchant_id = 'merchant_456'

-- Employee视角：看所有订单
SELECT * FROM orders
```

### agent_usage_logs记录所有API活动
```sql
-- 单个Agent的API使用
SELECT * FROM agent_usage_logs WHERE agent_id = 'agent_123'

-- 按端点统计
SELECT endpoint, COUNT(*) FROM agent_usage_logs GROUP BY endpoint

-- 错误率分析
SELECT status_code, COUNT(*) FROM agent_usage_logs GROUP BY status_code
```

## 🚨 当前问题

### 1. orders.agent_id字段未正确填充
- **影响**: Agent Portal的订单统计不准确
- **原因**: 创建订单时agent_id存在metadata中，未提取到主字段
- **解决**: 修改order创建逻辑，从metadata提取agent_id

### 2. 缺少total_gmv列
- **影响**: Agent metrics报错
- **原因**: agents表结构未更新
- **解决**: ALTER TABLE agents ADD COLUMN total_gmv DECIMAL(12,2)

### 3. 产品同步后不显示
- **影响**: Merchant看不到同步的产品
- **原因**: products_cache的expires_at字段问题
- **解决**: 确保v2端点正确过滤过期产品

## 📊 Metrics最佳实践

### 1. 数据一致性
- 所有Portal从同一数据源读取
- 使用数据库视图统一计算逻辑
- 避免在前端计算关键指标

### 2. 性能优化
- 使用物化视图缓存汇总数据
- 为常用查询添加索引
- 限制历史数据查询范围

### 3. 权限控制
- Agent只能看自己的数据
- Merchant只能看自己店铺数据
- Employee可以看所有数据

## 🔧 推荐改进

### 1. 创建统一的Metrics服务
```python
class MetricsService:
    async def get_agent_metrics(agent_id: str):
        # 统一计算逻辑
        
    async def get_merchant_metrics(merchant_id: str):
        # 统一计算逻辑
        
    async def get_platform_metrics():
        # 统一计算逻辑
```

### 2. 实现数据缓存层
- Redis缓存热点数据
- 定时更新汇总指标
- 减少数据库查询压力

### 3. 添加数据验证
- 确保agent_id总是被正确记录
- 验证订单金额计算
- 监控数据异常




