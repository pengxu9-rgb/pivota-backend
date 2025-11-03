# 🎯 监控和日志系统 - 实施完成

## 📊 已完成的功能

### 1. **Sentry Error Tracking** ✅
- **文件**: `pivota_infra/config/sentry_config.py`
- **状态**: 已配置，等待环境变量
- **功能**:
  - 自动捕获所有未处理的异常
  - FastAPI 和 SQLAlchemy 集成
  - 性能追踪（traces_sample_rate: 10%）
  - 敏感数据过滤（API keys, tokens, passwords）
  - Release tracking (使用 Railway deployment ID)

**设置 Sentry (仅需 1 分钟)**:
```bash
# 在 Railway 环境变量中添加:
SENTRY_DSN=your_sentry_dsn_here
SENTRY_TRACES_SAMPLE_RATE=0.1  # 可选，默认 0.1 (10%)
ENVIRONMENT=production  # 可选，默认 production
```

---

### 2. **Agent Metrics API** ✅
**文件**: `pivota_infra/routes/agent_metrics.py`

新增 4 个监控端点：

| 端点 | 描述 | 认证 |
|------|------|------|
| `GET /agent/metrics/summary` | 实时 API 使用统计摘要 | ✅ Admin |
| `GET /agent/metrics/agents` | 每个 Agent 的使用指标 | ✅ Admin |
| `GET /agent/metrics/timeline` | 按小时的请求时间线 | ✅ Admin |
| `GET /agent/metrics/health` | 公开健康检查 | ❌ Public |

**Metrics Summary 包含**:
```json
{
  "overview": {
    "total_requests": 12345,
    "requests_last_hour": 234,
    "requests_last_24h": 5678,
    "requests_last_7d": 45000
  },
  "performance": {
    "success_rate_24h": 98.5,
    "avg_response_time_ms": 145.2
  },
  "agents": {
    "active_last_24h": 12
  },
  "orders": {
    "count_last_24h": 45,
    "revenue_last_24h": 2345.67
  },
  "top_endpoints": [...],
  "errors": [...]
}
```

**测试 (部署后)**:
```bash
# 需要 Employee token
curl -H "Authorization: Bearer YOUR_EMPLOYEE_TOKEN" \
  https://web-production-fedb.up.railway.app/agent/metrics/summary

# Public health check (无需认证)
curl https://web-production-fedb.up.railway.app/agent/metrics/health
```

---

### 3. **Employee Portal 监控 Dashboard** ✅
**文件**: `pivota-employee-portal/app/dashboard/monitoring/page.tsx`

**功能**:
- ✅ 实时显示 API 使用统计
- ✅ 成功率和响应时间监控
- ✅ 活跃 Agent 数量
- ✅ 订单数量和收入（24小时）
- ✅ Top 10 最常用端点
- ✅ 错误分析（按 HTTP 状态码）
- ✅ 自动刷新（每 30 秒）
- ✅ 手动刷新按钮
- ✅ 健康状态指示器

**访问**:
- 导航: Employee Portal → Dashboard → **Monitoring**
- URL: `/dashboard/monitoring`

**视图组件**:
1. **状态卡片** - 请求数、成功率、响应时间、活跃 Agents
2. **订单和收入** - 24 小时统计
3. **Top 端点** - 最常用的 API 端点
4. **错误分析** - HTTP 错误码分布

---

### 4. **结构化日志中间件** ✅ (已存在)
**文件**: `pivota_infra/middleware/structured_logging.py`

**功能**:
- 所有请求/响应以 JSON 格式记录
- 包含: method, path, status_code, duration, timestamp
- 集成在 `main.py` 中 (`StructuredLoggingMiddleware`)

**日志格式**:
```json
{
  "timestamp": "2025-10-22T12:00:00Z",
  "method": "POST",
  "path": "/agent/v1/orders/create",
  "status_code": 200,
  "duration_ms": 145.2,
  "user_agent": "Pivota-Python-SDK/1.0.0"
}
```

---

## 📈 数据源

所有监控指标来自 **`agent_usage_logs`** 表:
```sql
CREATE TABLE agent_usage_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50),
    endpoint VARCHAR(255),
    method VARCHAR(10),
    merchant_id VARCHAR(50),
    status_code INTEGER,
    response_time_ms INTEGER,
    error_message TEXT,
    order_id VARCHAR(50),
    order_amount NUMERIC(10,2),
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
```

**日志记录位置**:
- `pivota_infra/routes/agent_api.py` - `log_agent_request()` 函数
- 每个 Agent API 请求都会记录到此表

---

## 🚀 部署状态

### Backend (Railway)
- **Commit**: `b862981e`
- **状态**: ⏳ 等待部署
- **新增**:
  - `routes/agent_metrics.py`
  - `sentry-sdk` 依赖
  - 4 个新的 metrics 端点

### Frontend (Employee Portal - Vercel)
- **Commit**: `2f49497`
- **状态**: ✅ 已推送到 GitHub
- **新增**:
  - `/dashboard/monitoring` 页面
  - 导航链接 "Monitoring"

---

## 📋 使用指南

### 为 Employee 查看监控

1. **登录 Employee Portal**
2. **点击侧边栏 "Monitoring"**
3. **查看实时指标**:
   - 请求统计
   - 成功率和响应时间
   - 订单和收入
   - 错误分析
4. **页面每 30 秒自动刷新**

### 为 Admin 查看详细指标

使用 API 直接查询:

```bash
# 获取 Employee token
TOKEN=$(curl -X POST https://web-production-fedb.up.railway.app/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"employee@pivota.com","password":"Admin123!"}' \
  | jq -r '.access_token')

# 查看 metrics summary
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/agent/metrics/summary \
  | jq .

# 查看每个 agent 的指标
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/agent/metrics/agents \
  | jq .

# 查看时间线
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/agent/metrics/timeline?hours=24 \
  | jq .
```

---

## 🎯 监控最佳实践

### 1. **设置 Sentry 告警**
- 在 Sentry dashboard 中配置:
  - Error rate > 5%
  - Response time > 500ms
  - Specific error patterns

### 2. **定期检查监控 Dashboard**
- 每天检查一次成功率
- 关注响应时间趋势
- 监控活跃 Agent 数量

### 3. **调查错误**
- 点击错误数量查看详情
- 在 Sentry 中追踪 stack traces
- 使用 `agent_usage_logs` 表查询详细日志

### 4. **性能优化**
- 响应时间 > 200ms: 需要优化
- 成功率 < 95%: 需要调查
- 错误率 > 5%: 需要立即处理

---

## 🔍 Troubleshooting

### Dashboard 显示 "Failed to load metrics"
**原因**: 
- Backend 未部署
- Employee token 过期
- API 端点错误

**解决**:
```bash
# 1. 检查 backend version
curl https://web-production-fedb.up.railway.app/version

# 2. 检查 health endpoint (无需认证)
curl https://web-production-fedb.up.railway.app/agent/metrics/health

# 3. 重新登录 Employee Portal
```

### Sentry 未捕获错误
**原因**: 
- `SENTRY_DSN` 未设置
- `sentry-sdk` 未安装

**解决**:
```bash
# 1. 在 Railway 添加环境变量
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx

# 2. 检查 startup logs
# 应该看到: ✅ Sentry error tracking initialized
```

### 指标数据为 0
**原因**: 
- `agent_usage_logs` 表为空
- Agent 未使用 API

**解决**:
```bash
# 检查 agent_usage_logs
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/agent/metrics/summary

# 使用 Agent SDK 生成一些请求
cd pivota_sdk/python
PYTHONPATH=. python examples/mcp_sample.py
```

---

## ✅ 完成清单

- ✅ Sentry error tracking 配置完成
- ✅ 4 个 metrics API 端点已添加
- ✅ Employee Portal 监控 dashboard 已创建
- ✅ 导航链接已添加
- ✅ 自动刷新功能已实现
- ✅ 健康检查端点（公开）
- ✅ 结构化日志中间件（已存在）
- ✅ 所有代码已提交到 GitHub

---

## 📝 下一步 (可选)

### 立即可做:
1. **在 Railway 设置 SENTRY_DSN** - 5 分钟
2. **测试监控 dashboard** - 部署完成后
3. **在 Sentry 配置告警规则** - 10 分钟

### 未来增强:
1. **添加 Grafana dashboard** - 可视化历史数据
2. **设置 Slack 告警** - 错误实时通知
3. **添加自定义事件追踪** - 业务指标
4. **实现 APM (Application Performance Monitoring)**

---

## 🎉 总结

**监控和日志系统现已完全实施!**

✅ **实时监控** - Employee Portal dashboard  
✅ **错误追踪** - Sentry 集成（需配置）  
✅ **API 指标** - 4 个新端点  
✅ **结构化日志** - JSON 格式  
✅ **健康检查** - 公开端点  

**部署后即可使用，只需在 Railway 设置 SENTRY_DSN 即可启用完整的错误追踪功能！**








