# Agent API 迁移指南

## ⚠️ 重要通知

旧版 Agent API (`/agent/*`) 已被弃用，将于 **2026-05-01** 完全移除。
请迁移到新版 API (`/agent/v1/*`)。

## 📋 端点映射

### 订单创建
```diff
- POST /agent/pay
+ POST /agent/v1/orders/create
```

**请求格式变更**：
```diff
旧版本:
{
  "agent_id": "agent_123",
  "merchant_id": "merchant_456", 
  "amount": 100.00,
  "currency": "USD"
}

新版本:
{
  "merchant_id": "merchant_456",
  "items": [
    {
      "product_id": "prod_123",
      "product_title": "Product Name",
      "quantity": 1,
      "unit_price": 100.00,
      "subtotal": 100.00
    }
  ],
  "customer_email": "customer@example.com",
  "shipping_address": {
    "name": "Customer Name",
    "address_line1": "123 Main St",
    "city": "New York",
    "state": "NY",
    "postal_code": "10001",
    "country": "US"
  }
}
```

### 支付确认
```diff
- POST /agent/confirm/{order_id}  (不存在)
+ POST /agent/v1/orders/{order_id}/confirm-payment
```

### PSP 性能指标
```diff
- GET /agent/psp-metrics  
+ 已移除 - 使用内部监控系统
```

## 🔑 认证方式变更

### 旧版本
```http
Authorization: Bearer {jwt_token}
```

### 新版本
```http
x-api-key: {agent_api_key}
```

## 📊 响应格式变更

### 旧版本响应
```json
{
  "payment_intent": "pi_123",
  "client_secret": "pi_123_secret",
  "order_id": "ord_456",
  "psp": "stripe",
  "ai_score": 95.5,
  "latency_ms": 200
}
```

### 新版本响应
```json
{
  "status": "success",
  "order_id": "ORD_1234567890",
  "total": "100.00",
  "currency": "USD",
  "payment": {
    "client_secret": "pi_123_secret",
    "payment_intent_id": "pi_123",
    "instructions": "Use client_secret for Stripe payment confirmation"
  },
  "tracking": {
    "agent_session_id": "agent_123_1234567890",
    "created_at": "2025-11-06T12:00:00.000Z"
  }
}
```

## 🚀 迁移步骤

1. **更新 SDK/客户端代码**
   - 更新端点 URL
   - 更改请求格式
   - 更新认证头

2. **测试新端点**
   ```bash
   # 测试订单创建
   curl -X POST https://api.pivota.cc/agent/v1/orders/create \
     -H "x-api-key: YOUR_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{...}'
   ```

3. **监控弃用端点调用**
   - 查看日志中的 "DEPRECATED API CALL" 警告
   - 跟踪哪些客户端仍在使用旧版本

4. **逐步切换**
   - 先在测试环境验证
   - 监控错误率
   - 确认功能正常后切换生产环境

## 🔧 常见问题

### Q: 为什么要迁移？
A: 新版本提供：
- 更完整的订单信息
- 更好的错误处理
- 自动佣金计算
- 与其他系统更好的集成

### Q: 旧版本还能用多久？
A: 到 2026-05-01，之后会返回 410 Gone 错误

### Q: 如何获取新的 API Key？
A: 通过 Agent Portal 或联系技术支持

## 📞 需要帮助？

- 技术文档：https://docs.pivota.cc/agent-api
- 技术支持：tech@pivota.cc
- Discord: #agent-api-migration
