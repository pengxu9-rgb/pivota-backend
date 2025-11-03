# Dashboard Analytics 修复

## 🐛 问题

1. **Recent API Activity 没有数据** - 列表为空
2. **Analytics Dashboard 没有动态** - 所有数据都是静态 mock 数据
3. **无法追踪真实的 API 调用**

## 🔍 根本原因

**API 调用没有被记录到 `agent_usage_logs` 表**

- 缺少中间件来自动记录 Agent API 调用
- 所有 Dashboard 数据都依赖 `agent_usage_logs` 表
- 表是空的，所以 Dashboard 显示的都是 fallback mock 数据

## ✅ 解决方案

### 1. 创建 UsageLoggerMiddleware

新文件: `pivota_infra/middleware/usage_logger.py`

功能：
- 自动拦截所有 `/agent/v1/*` 的请求
- 提取 API key 并查找对应的 `agent_id`
- 记录以下信息到 `agent_usage_logs` 表：
  - `agent_id` - 哪个 agent 调用的
  - `endpoint` - 调用的端点
  - `method` - HTTP 方法（GET/POST/etc）
  - `status_code` - 响应状态码
  - `response_time_ms` - 响应时间（毫秒）
  - `timestamp` - 调用时间

### 2. 添加到 FastAPI 中间件栈

在 `main.py` 中：
```python
# Add usage logging middleware (tracks Agent API calls)
app.add_middleware(UsageLoggerMiddleware)
```

放在最前面，确保所有请求都被记录。

## 📊 现在会自动记录的数据

### Recent API Activity
每次调用 Agent API 都会自动记录：
```json
{
  "agent_id": "agent@test.com",
  "endpoint": "/agent/v1/products/search",
  "method": "GET",
  "status_code": 200,
  "response_time_ms": 245,
  "timestamp": "2025-10-24T..."
}
```

### Analytics Dashboard
可以统计：
- **Product Searches**: 搜索产品的次数
- **Inventory Checks**: 检查库存的次数
- **Price Queries**: 价格查询的次数
- **Order Creation**: 创建订单的次数
- **Response Times**: 平均/最大响应时间
- **Success Rate**: 成功率（200 vs 400/500）

## 🔄 部署后效果

部署完成后（约 2 分钟），当有 API 调用时：

1. **Recent API Activity** 会立即显示新的活动
2. **MCP Query Analytics** 会显示真实的查询统计
3. **API Calls Today** 会实时更新
4. **Order Conversion Funnel** 会显示真实的转化数据

## 🧪 测试方法

部署完成后，运行几个测试订单：
```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344
python3 quick_test_orders.py
```

这会：
1. 搜索产品（1 次 API 调用）
2. 创建 5 个订单（5 次 API 调用）
3. 总共 6 条记录插入到 `agent_usage_logs`

然后访问 https://agents.pivota.cc/dashboard，应该看到：
- ✅ "API Calls Today" 显示 6+
- ✅ "Recent API Activity" 显示 6 条记录
- ✅ "MCP Query Analytics" 显示 1 次产品搜索
- ✅ 响应时间数据

## 📝 数据库 Schema

如果 `agent_usage_logs` 表不存在，需要创建：
```sql
CREATE TABLE IF NOT EXISTS agent_usage_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    endpoint VARCHAR(500) NOT NULL,
    method VARCHAR(10),
    status_code INTEGER,
    response_time_ms INTEGER,
    timestamp TIMESTAMP DEFAULT NOW(),
    request_id VARCHAR(100)
);

-- 创建索引以提升查询性能
CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id ON agent_usage_logs(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_timestamp ON agent_usage_logs(timestamp DESC);
```

## 🎯 下一步

1. **等待部署完成**（约 2 分钟）
2. **运行测试脚本** 生成一些 API 调用
3. **刷新 Dashboard** 查看真实数据
4. **验证 Analytics 页面** 确保图表显示正确

所有数据现在都会自动记录并显示！




