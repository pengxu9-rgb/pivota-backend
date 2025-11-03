# Phase 4: Payment Routing & Protocol Support - 用户指南

## 🎯 功能概述

Phase 4 为 Pivota 平台提供了智能支付路由和多协议支持能力，确保支付请求的高可用性和灵活性。

## ✨ 核心功能

### 1. 🔄 智能支付路由
- **优先级路由**：按配置顺序使用 PSP
- **自动故障转移**：主 PSP 失败时自动切换到备用 PSP
- **性能优化**：基于历史数据选择最佳 PSP（未来功能）
- **成本优化**：根据交易费用选择最便宜的 PSP（未来功能）

### 2. 🔌 协议支持
- **AP2** (Agent Payment Protocol v2) - 标准支付协议
- **ACP** (Agent Commerce Protocol) - 完整电商协议
- **X-402** (Extended Payment Protocol) - 高级多币种协议

### 3. 📊 实时监控
- PSP 性能指标追踪
- 故障转移事件记录
- 路由效率分析
- 关键告警通知

## 🚀 快速开始

### Agent 端使用

#### 1. 查看您的路由配置
```bash
curl https://web-production-fedb.up.railway.app/agents/{YOUR_AGENT_ID}/routes \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**示例响应：**
```json
[{
  "route_id": "route_1a49ca1d31bc",
  "psp_priority": [
    {"psp": "stripe", "priority": 1},
    {"psp": "adyen", "priority": 2},
    {"psp": "paypal", "priority": 3}
  ],
  "routing_strategy": "priority",
  "max_retries": 2
}]
```

#### 2. 使用路由创建支付
```bash
curl -X POST https://web-production-fedb.up.railway.app/agents/{YOUR_AGENT_ID}/payments/route \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "order_001",
    "amount": 100.00,
    "currency": "USD",
    "merchant_id": "merchant_123"
  }'
```

**系统会自动：**
1. 选择最佳 PSP（根据配置和性能）
2. 尝试主要 PSP (Stripe)
3. 如果失败，自动切换到 Adyen
4. 如果仍失败，尝试 PayPal
5. 记录所有尝试供监控使用

#### 3. 查看支付尝试历史
```bash
curl https://web-production-fedb.up.railway.app/payments/{ORDER_ID}/attempts \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 协议使用指南

#### AP2 协议示例

**创建支付：**
```json
{
  "order_id": "order_123",
  "amount": 100.00,
  "currency": "USD",
  "merchant_id": "merchant_456",
  "customer": {
    "email": "customer@example.com",
    "name": "John Doe"
  }
}
```

**验证载荷：**
```bash
curl -X POST https://web-production-fedb.up.railway.app/protocols/AP2/validate \
  -H "Content-Type: application/json" \
  -d '{"payload": YOUR_PAYLOAD}'
```

#### ACP 协议示例

**创建订单：**
```json
{
  "agent_id": "agent_123",
  "merchant_id": "merchant_456",
  "items": [
    {
      "sku": "PROD-001",
      "name": "Product Name",
      "quantity": 2,
      "price": 50.00
    }
  ],
  "customer": {
    "email": "customer@example.com",
    "name": "John Doe",
    "address": {
      "line1": "123 Main St",
      "city": "San Francisco",
      "state": "CA",
      "zip": "94102"
    }
  },
  "shipping": {
    "method": "standard",
    "cost": 5.00
  }
}
```

#### X-402 协议示例

**授权支付：**
```json
{
  "transaction_id": "txn_123",
  "amount": 200.00,
  "currency": "USD",
  "authorization_code": "AUTH123456",
  "capture_mode": "immediate",
  "currencies": ["USD", "EUR", "GBP"]
}
```

## 🎛️ Employee Portal 管理

### 查看 Agent 路由配置

1. 访问：https://employee.pivota.cc/dashboard/agents
2. 点击任意 Agent 查看详情
3. 展开 "Payment Routing & Failover" 部分

**可以看到：**
- PSP 优先级顺序
- 最近的支付尝试
- 成功率统计
- 故障转移历史

### 测试协议

1. 在 Agent 详情中展开 "Protocols Support"
2. 选择协议（AP2, ACP, 或 X-402）
3. 编辑测试载荷
4. 点击 "Run Test"

**查看结果：**
- 验证状态
- 转换后的请求格式
- 模拟响应
- 可用端点列表

### 监控 PSP 性能

**访问路由监控：**
- Employee Portal > PSP Overview
- 实时查看所有 PSP 的性能指标
- 监控故障转移事件
- 设置告警阈值

## 📊 API 端点完整列表

### Payment Routing

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| POST | `/agents/{id}/payments/route` | 执行带路由的支付 | Agent |
| GET | `/agents/{id}/routes` | 获取路由配置 | Agent |
| PUT | `/agents/{id}/routes/{route_id}` | 更新路由配置 | Agent |
| POST | `/agents/{id}/routes` | 创建新路由 | Agent |
| DELETE | `/agents/{id}/routes/{route_id}` | 删除路由 | Agent |
| GET | `/payments/{id}/attempts` | 查看支付尝试 | Agent |

### Protocol Management

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/protocols/` | 列出所有协议 | Public |
| POST | `/protocols/{name}/validate` | 验证载荷 | Public |
| GET | `/agents/{id}/protocols/` | 获取 Agent 协议 | Agent |
| POST | `/agents/{id}/protocols/` | 启用协议 | Admin |
| DELETE | `/agents/{id}/protocols/{name}` | 禁用协议 | Admin |
| POST | `/agents/{id}/protocols/{name}/test` | 测试协议 | Agent |
| GET | `/agents/{id}/protocols/{name}/events` | 协议事件日志 | Agent |

### Employee Monitoring

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/employee/psp/performance` | PSP 性能指标 | Employee |
| GET | `/employee/psp/routes/overview` | 路由概览 | Employee |
| GET | `/employee/psp/failovers` | 故障转移事件 | Employee |
| POST | `/employee/psp/routes/{id}/test` | 测试路由 | Employee |
| GET | `/employee/psp/metrics/realtime` | 实时指标 | Employee |
| POST | `/employee/psp/metrics/collect` | 手动收集指标 | Employee |
| GET | `/employee/psp/alerts/active` | 活跃告警 | Employee |
| GET | `/employee/protocols/usage-stats` | 协议使用统计 | Employee |
| GET | `/employee/protocols/adoption` | 协议采用率 | Employee |

## 🔧 配置管理

### 更新路由优先级

```bash
curl -X PUT https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/routes/{ROUTE_ID} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "psp_priority": [
      {"psp": "adyen", "priority": 1},
      {"psp": "stripe", "priority": 2},
      {"psp": "paypal", "priority": 3}
    ],
    "routing_strategy": "priority",
    "max_retries": 3,
    "timeout_ms": 45000
  }'
```

### 为 Agent 启用新协议

```bash
curl -X POST "https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/protocols/?protocol_name=X-402&version=3.1" \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

## 📈 监控和告警

### 查看实时 PSP 性能

```bash
curl https://web-production-fedb.up.railway.app/employee/psp/performance \
  -H "Authorization: Bearer EMPLOYEE_TOKEN" | python3 -m json.tool
```

**响应示例：**
```json
{
  "psp_name": "stripe",
  "current_status": "healthy",
  "success_rate_5min": 98.5,
  "success_rate_1h": 97.2,
  "avg_response_time_ms": 250,
  "total_attempts_1h": 150,
  "failover_triggered_count": 2,
  "alerts": []
}
```

### WebSocket 实时告警

连接到 WebSocket 接收实时告警：

```javascript
import { useWebSocket } from '@/lib/websocket-client';

const ws = useWebSocket();

// 监听 PSP 故障
ws.on('psp_failure', (alert) => {
  console.error('PSP Failure:', alert);
  // 显示通知给用户
});

// 监听高失败率
ws.on('high_failure_rate', (alert) => {
  console.warn('High Failure Rate:', alert);
});

// 监听支付故障转移
ws.on('payment_failover', (event) => {
  console.log('Payment Failover:', event);
});
```

## 🧪 测试场景

### 场景 1: 正常支付流程

```bash
# 1. 创建支付
curl -X POST https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/payments/route \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "test_normal_001",
    "amount": 50.00,
    "currency": "USD"
  }'

# 预期结果:
# - 使用 Stripe (priority 1)
# - 支付成功
# - response_time_ms < 1000ms
```

### 场景 2: 故障转移测试

```bash
# 当主 PSP 失败时，系统自动尝试备用 PSP
# 查看尝试历史：

curl https://web-production-fedb.up.railway.app/payments/{ORDER_ID}/attempts \
  -H "Authorization: Bearer YOUR_TOKEN"

# 预期响应:
# [
#   {
#     "attempt_number": 1,
#     "psp_name": "stripe",
#     "status": "failed",
#     "error_message": "..."
#   },
#   {
#     "attempt_number": 2,
#     "psp_name": "adyen",
#     "status": "success",
#     "response_time_ms": 350
#   }
# ]
```

### 场景 3: 协议测试沙盒

```bash
# 测试 ACP 协议
curl -X POST https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/protocols/ACP/test \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol": "ACP",
    "test_payload": {
      "agent_id": "agent_123",
      "merchant_id": "merchant_456",
      "items": [
        {"sku": "PROD-001", "quantity": 1, "price": 50.00}
      ],
      "customer": {
        "email": "test@example.com",
        "name": "Test User"
      }
    }
  }'

# 预期结果:
# - 验证通过
# - 请求转换为内部格式
# - 返回模拟响应
# - 显示可用端点
```

## 📱 Employee Portal UI 功能

### Agent Detail Panel 新增部分

#### 1. **Payment Routing & Failover**
- PSP 优先级列表（可拖拽排序）
- 最近支付尝试表格
- 成功率统计
- 配置编辑功能

#### 2. **Protocols Support**
- 已启用协议列表
- 协议版本信息
- 状态标识（Active/Beta/Deprecated）
- 协议测试沙盒

#### 3. **Protocol Test Sandbox**
- 协议选择下拉菜单
- JSON 载荷编辑器
- 一键测试按钮
- 验证结果显示
- 转换后的请求/响应查看

### PSP Performance Dashboard

访问：Employee Portal > Dashboard

**功能：**
- 实时 PSP 状态卡片
- 性能趋势图表
- 故障转移历史
- 关键告警横幅
- 自动刷新（30秒）

## 🎯 路由策略说明

### Priority (优先级) - 默认策略
按配置的优先级顺序使用 PSP：
```
请求 → Stripe (优先级1)
  ↓ 失败
Adyen (优先级2)
  ↓ 失败
PayPal (优先级3)
```

### Cost (成本优化) - 未来功能
根据交易费用选择最便宜的 PSP：
- 比较固定费用
- 计算百分比费用
- 考虑汇率转换
- 评估批量折扣

### Performance (性能优化) - 未来功能
基于历史表现选择最快的 PSP：
- 平均响应时间
- 成功率
- 近期表现趋势
- 地理位置优化

## 🔔 告警系统

### 自动告警触发条件

| 告警类型 | 触发条件 | 严重程度 |
|---------|---------|---------|
| high_failure_rate | 失败率 > 30% | Critical/Warning |
| high_latency | 响应时间 > 5000ms | Warning |
| psp_down | 5分钟内无成功请求 | Warning |
| unusual_spike | 流量 > 基线3倍 | Warning |

### WebSocket 实时通知

关键告警通过 WebSocket 实时推送：
- PSP 故障
- 高失败率
- 支付故障转移
- 性能降级

## 📊 性能指标

### 收集的指标

**PSP 级别：**
- 总尝试次数
- 成功/失败/超时次数
- 平均响应时间
- P95/P99 响应时间
- 故障转移触发次数

**Route 级别：**
- 总支付次数
- 成功率
- 平均尝试次数
- PSP 使用分布
- 效率评分

**Protocol 级别：**
- 总事件数
- 唯一 Agent 数
- 平均响应时间
- 验证失败次数

### 指标更新频率

- **实时指标**：每 5 分钟收集一次
- **UI 更新**：30 秒轮询 + WebSocket 推送
- **历史数据**：保留 30 天

## 🛠️ 故障排查

### 常见问题

#### 1. 支付一直失败
**检查：**
```bash
# 查看支付尝试详情
curl https://web-production-fedb.up.railway.app/payments/{ORDER_ID}/attempts \
  -H "Authorization: Bearer YOUR_TOKEN"

# 检查 PSP 状态
curl https://web-production-fedb.up.railway.app/employee/psp/performance \
  -H "Authorization: Bearer EMPLOYEE_TOKEN"
```

**可能原因：**
- 所有 PSP 都失败（检查 PSP 配置）
- 金额超出限额
- 币种不支持
- API 密钥无效

#### 2. 协议验证失败
**检查：**
```bash
# 验证载荷格式
curl -X POST https://web-production-fedb.up.railway.app/protocols/{PROTOCOL}/validate \
  -H "Content-Type: application/json" \
  -d '{"payload": YOUR_PAYLOAD}'
```

**常见错误：**
- 缺少必填字段
- 金额格式错误（必须是数字）
- 币种代码错误（必须是3位ISO代码）
- Items 数组为空（ACP协议）

#### 3. 路由配置不生效
**检查：**
- 路由是否激活（`is_active: true`）
- PSP 名称拼写是否正确
- 优先级数字是否唯一
- Max retries 设置合理（1-5）

## 🚀 高级功能（未来）

### 即将推出

- **成本优化路由**：自动选择最便宜的 PSP
- **性能优化路由**：基于实时表现选择最快的 PSP
- **地理路由**：根据客户位置选择本地 PSP
- **金额分层**：大额小额使用不同路由策略
- **VCC 支持**：虚拟卡支付
- **Stablecoin 支付**：加密货币结算
- **ACH 直连**：银行直接转账

### 预留接口

所有服务都包含 TODO 标记，为未来功能预留接口：
```python
# TODO(phase5): Implement cost-based routing
# TODO(phase5): Add VCC payment support
# TODO(phase5): Integrate stablecoin settlements
```

## 📖 协议规范文档

### AP2 v2.0 规范

**请求格式：**
```typescript
interface AP2PaymentRequest {
  order_id: string;
  amount: number;
  currency: string; // ISO 4217
  merchant_id: string;
  customer?: {
    email: string;
    name: string;
  };
}
```

**响应格式：**
```typescript
interface AP2PaymentResponse {
  transaction_id: string;
  status: 'success' | 'failed' | 'pending';
  order_id: string;
  amount: number;
  currency: string;
  psp_used: string;
  created_at: string;
  protocol: {
    name: 'AP2';
    version: '2.0';
  };
}
```

### ACP v1.0 规范

**请求格式：**
```typescript
interface ACPOrderRequest {
  agent_id: string;
  merchant_id: string;
  items: Array<{
    sku: string;
    name?: string;
    quantity: number;
    price: number;
  }>;
  customer: {
    email: string;
    name: string;
    address?: Address;
  };
  shipping?: {
    method: string;
    cost: number;
  };
}
```

### X-402 v3.1 规范

**请求格式：**
```typescript
interface X402AuthRequest {
  transaction_id: string;
  amount: number;
  currency: string;
  authorization_code: string;
  capture_mode: 'immediate' | 'manual';
  currencies?: string[]; // 多币种支持
}
```

## ✅ 成功案例

### 案例 1: 高可用支付

**场景：** Stripe 临时故障

```
请求 → Stripe (失败，响应时间: timeout)
     → Adyen (成功，响应时间: 450ms)
结果：支付成功完成，用户无感知
```

### 案例 2: 协议兼容性

**场景：** Agent 使用不同协议

```
Agent A 使用 AP2 → 标准支付流程
Agent B 使用 ACP → 完整电商流程
Agent C 使用 X-402 → 多币种高级功能
结果：所有 Agent 都能无缝集成
```

## 🎓 最佳实践

### 1. 路由配置
- ✅ 设置至少 2 个备用 PSP
- ✅ 将最可靠的 PSP 设为优先级 1
- ✅ Max retries 建议设为 2-3
- ✅ Timeout 根据业务需求设置（30-60秒）

### 2. 协议选择
- **AP2**: 简单支付场景
- **ACP**: 完整电商订单
- **X-402**: 需要多币种或高级功能

### 3. 监控
- ✅ 定期检查 PSP 性能指标
- ✅ 关注故障转移频率
- ✅ 订阅关键告警
- ✅ 定期优化路由配置

---

## 🎉 Phase 4 完整功能已就绪！

所有组件都已部署并验证通过：
- ✅ 智能支付路由
- ✅ 自动故障转移
- ✅ 协议支持系统
- ✅ 实时性能监控
- ✅ Employee Portal 集成

**开始使用 Phase 4 功能，提升支付系统的可靠性和灵活性！** 🚀

## 🎯 功能概述

Phase 4 为 Pivota 平台提供了智能支付路由和多协议支持能力，确保支付请求的高可用性和灵活性。

## ✨ 核心功能

### 1. 🔄 智能支付路由
- **优先级路由**：按配置顺序使用 PSP
- **自动故障转移**：主 PSP 失败时自动切换到备用 PSP
- **性能优化**：基于历史数据选择最佳 PSP（未来功能）
- **成本优化**：根据交易费用选择最便宜的 PSP（未来功能）

### 2. 🔌 协议支持
- **AP2** (Agent Payment Protocol v2) - 标准支付协议
- **ACP** (Agent Commerce Protocol) - 完整电商协议
- **X-402** (Extended Payment Protocol) - 高级多币种协议

### 3. 📊 实时监控
- PSP 性能指标追踪
- 故障转移事件记录
- 路由效率分析
- 关键告警通知

## 🚀 快速开始

### Agent 端使用

#### 1. 查看您的路由配置
```bash
curl https://web-production-fedb.up.railway.app/agents/{YOUR_AGENT_ID}/routes \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**示例响应：**
```json
[{
  "route_id": "route_1a49ca1d31bc",
  "psp_priority": [
    {"psp": "stripe", "priority": 1},
    {"psp": "adyen", "priority": 2},
    {"psp": "paypal", "priority": 3}
  ],
  "routing_strategy": "priority",
  "max_retries": 2
}]
```

#### 2. 使用路由创建支付
```bash
curl -X POST https://web-production-fedb.up.railway.app/agents/{YOUR_AGENT_ID}/payments/route \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "order_001",
    "amount": 100.00,
    "currency": "USD",
    "merchant_id": "merchant_123"
  }'
```

**系统会自动：**
1. 选择最佳 PSP（根据配置和性能）
2. 尝试主要 PSP (Stripe)
3. 如果失败，自动切换到 Adyen
4. 如果仍失败，尝试 PayPal
5. 记录所有尝试供监控使用

#### 3. 查看支付尝试历史
```bash
curl https://web-production-fedb.up.railway.app/payments/{ORDER_ID}/attempts \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 协议使用指南

#### AP2 协议示例

**创建支付：**
```json
{
  "order_id": "order_123",
  "amount": 100.00,
  "currency": "USD",
  "merchant_id": "merchant_456",
  "customer": {
    "email": "customer@example.com",
    "name": "John Doe"
  }
}
```

**验证载荷：**
```bash
curl -X POST https://web-production-fedb.up.railway.app/protocols/AP2/validate \
  -H "Content-Type: application/json" \
  -d '{"payload": YOUR_PAYLOAD}'
```

#### ACP 协议示例

**创建订单：**
```json
{
  "agent_id": "agent_123",
  "merchant_id": "merchant_456",
  "items": [
    {
      "sku": "PROD-001",
      "name": "Product Name",
      "quantity": 2,
      "price": 50.00
    }
  ],
  "customer": {
    "email": "customer@example.com",
    "name": "John Doe",
    "address": {
      "line1": "123 Main St",
      "city": "San Francisco",
      "state": "CA",
      "zip": "94102"
    }
  },
  "shipping": {
    "method": "standard",
    "cost": 5.00
  }
}
```

#### X-402 协议示例

**授权支付：**
```json
{
  "transaction_id": "txn_123",
  "amount": 200.00,
  "currency": "USD",
  "authorization_code": "AUTH123456",
  "capture_mode": "immediate",
  "currencies": ["USD", "EUR", "GBP"]
}
```

## 🎛️ Employee Portal 管理

### 查看 Agent 路由配置

1. 访问：https://employee.pivota.cc/dashboard/agents
2. 点击任意 Agent 查看详情
3. 展开 "Payment Routing & Failover" 部分

**可以看到：**
- PSP 优先级顺序
- 最近的支付尝试
- 成功率统计
- 故障转移历史

### 测试协议

1. 在 Agent 详情中展开 "Protocols Support"
2. 选择协议（AP2, ACP, 或 X-402）
3. 编辑测试载荷
4. 点击 "Run Test"

**查看结果：**
- 验证状态
- 转换后的请求格式
- 模拟响应
- 可用端点列表

### 监控 PSP 性能

**访问路由监控：**
- Employee Portal > PSP Overview
- 实时查看所有 PSP 的性能指标
- 监控故障转移事件
- 设置告警阈值

## 📊 API 端点完整列表

### Payment Routing

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| POST | `/agents/{id}/payments/route` | 执行带路由的支付 | Agent |
| GET | `/agents/{id}/routes` | 获取路由配置 | Agent |
| PUT | `/agents/{id}/routes/{route_id}` | 更新路由配置 | Agent |
| POST | `/agents/{id}/routes` | 创建新路由 | Agent |
| DELETE | `/agents/{id}/routes/{route_id}` | 删除路由 | Agent |
| GET | `/payments/{id}/attempts` | 查看支付尝试 | Agent |

### Protocol Management

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/protocols/` | 列出所有协议 | Public |
| POST | `/protocols/{name}/validate` | 验证载荷 | Public |
| GET | `/agents/{id}/protocols/` | 获取 Agent 协议 | Agent |
| POST | `/agents/{id}/protocols/` | 启用协议 | Admin |
| DELETE | `/agents/{id}/protocols/{name}` | 禁用协议 | Admin |
| POST | `/agents/{id}/protocols/{name}/test` | 测试协议 | Agent |
| GET | `/agents/{id}/protocols/{name}/events` | 协议事件日志 | Agent |

### Employee Monitoring

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/employee/psp/performance` | PSP 性能指标 | Employee |
| GET | `/employee/psp/routes/overview` | 路由概览 | Employee |
| GET | `/employee/psp/failovers` | 故障转移事件 | Employee |
| POST | `/employee/psp/routes/{id}/test` | 测试路由 | Employee |
| GET | `/employee/psp/metrics/realtime` | 实时指标 | Employee |
| POST | `/employee/psp/metrics/collect` | 手动收集指标 | Employee |
| GET | `/employee/psp/alerts/active` | 活跃告警 | Employee |
| GET | `/employee/protocols/usage-stats` | 协议使用统计 | Employee |
| GET | `/employee/protocols/adoption` | 协议采用率 | Employee |

## 🔧 配置管理

### 更新路由优先级

```bash
curl -X PUT https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/routes/{ROUTE_ID} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "psp_priority": [
      {"psp": "adyen", "priority": 1},
      {"psp": "stripe", "priority": 2},
      {"psp": "paypal", "priority": 3}
    ],
    "routing_strategy": "priority",
    "max_retries": 3,
    "timeout_ms": 45000
  }'
```

### 为 Agent 启用新协议

```bash
curl -X POST "https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/protocols/?protocol_name=X-402&version=3.1" \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

## 📈 监控和告警

### 查看实时 PSP 性能

```bash
curl https://web-production-fedb.up.railway.app/employee/psp/performance \
  -H "Authorization: Bearer EMPLOYEE_TOKEN" | python3 -m json.tool
```

**响应示例：**
```json
{
  "psp_name": "stripe",
  "current_status": "healthy",
  "success_rate_5min": 98.5,
  "success_rate_1h": 97.2,
  "avg_response_time_ms": 250,
  "total_attempts_1h": 150,
  "failover_triggered_count": 2,
  "alerts": []
}
```

### WebSocket 实时告警

连接到 WebSocket 接收实时告警：

```javascript
import { useWebSocket } from '@/lib/websocket-client';

const ws = useWebSocket();

// 监听 PSP 故障
ws.on('psp_failure', (alert) => {
  console.error('PSP Failure:', alert);
  // 显示通知给用户
});

// 监听高失败率
ws.on('high_failure_rate', (alert) => {
  console.warn('High Failure Rate:', alert);
});

// 监听支付故障转移
ws.on('payment_failover', (event) => {
  console.log('Payment Failover:', event);
});
```

## 🧪 测试场景

### 场景 1: 正常支付流程

```bash
# 1. 创建支付
curl -X POST https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/payments/route \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "test_normal_001",
    "amount": 50.00,
    "currency": "USD"
  }'

# 预期结果:
# - 使用 Stripe (priority 1)
# - 支付成功
# - response_time_ms < 1000ms
```

### 场景 2: 故障转移测试

```bash
# 当主 PSP 失败时，系统自动尝试备用 PSP
# 查看尝试历史：

curl https://web-production-fedb.up.railway.app/payments/{ORDER_ID}/attempts \
  -H "Authorization: Bearer YOUR_TOKEN"

# 预期响应:
# [
#   {
#     "attempt_number": 1,
#     "psp_name": "stripe",
#     "status": "failed",
#     "error_message": "..."
#   },
#   {
#     "attempt_number": 2,
#     "psp_name": "adyen",
#     "status": "success",
#     "response_time_ms": 350
#   }
# ]
```

### 场景 3: 协议测试沙盒

```bash
# 测试 ACP 协议
curl -X POST https://web-production-fedb.up.railway.app/agents/{AGENT_ID}/protocols/ACP/test \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol": "ACP",
    "test_payload": {
      "agent_id": "agent_123",
      "merchant_id": "merchant_456",
      "items": [
        {"sku": "PROD-001", "quantity": 1, "price": 50.00}
      ],
      "customer": {
        "email": "test@example.com",
        "name": "Test User"
      }
    }
  }'

# 预期结果:
# - 验证通过
# - 请求转换为内部格式
# - 返回模拟响应
# - 显示可用端点
```

## 📱 Employee Portal UI 功能

### Agent Detail Panel 新增部分

#### 1. **Payment Routing & Failover**
- PSP 优先级列表（可拖拽排序）
- 最近支付尝试表格
- 成功率统计
- 配置编辑功能

#### 2. **Protocols Support**
- 已启用协议列表
- 协议版本信息
- 状态标识（Active/Beta/Deprecated）
- 协议测试沙盒

#### 3. **Protocol Test Sandbox**
- 协议选择下拉菜单
- JSON 载荷编辑器
- 一键测试按钮
- 验证结果显示
- 转换后的请求/响应查看

### PSP Performance Dashboard

访问：Employee Portal > Dashboard

**功能：**
- 实时 PSP 状态卡片
- 性能趋势图表
- 故障转移历史
- 关键告警横幅
- 自动刷新（30秒）

## 🎯 路由策略说明

### Priority (优先级) - 默认策略
按配置的优先级顺序使用 PSP：
```
请求 → Stripe (优先级1)
  ↓ 失败
Adyen (优先级2)
  ↓ 失败
PayPal (优先级3)
```

### Cost (成本优化) - 未来功能
根据交易费用选择最便宜的 PSP：
- 比较固定费用
- 计算百分比费用
- 考虑汇率转换
- 评估批量折扣

### Performance (性能优化) - 未来功能
基于历史表现选择最快的 PSP：
- 平均响应时间
- 成功率
- 近期表现趋势
- 地理位置优化

## 🔔 告警系统

### 自动告警触发条件

| 告警类型 | 触发条件 | 严重程度 |
|---------|---------|---------|
| high_failure_rate | 失败率 > 30% | Critical/Warning |
| high_latency | 响应时间 > 5000ms | Warning |
| psp_down | 5分钟内无成功请求 | Warning |
| unusual_spike | 流量 > 基线3倍 | Warning |

### WebSocket 实时通知

关键告警通过 WebSocket 实时推送：
- PSP 故障
- 高失败率
- 支付故障转移
- 性能降级

## 📊 性能指标

### 收集的指标

**PSP 级别：**
- 总尝试次数
- 成功/失败/超时次数
- 平均响应时间
- P95/P99 响应时间
- 故障转移触发次数

**Route 级别：**
- 总支付次数
- 成功率
- 平均尝试次数
- PSP 使用分布
- 效率评分

**Protocol 级别：**
- 总事件数
- 唯一 Agent 数
- 平均响应时间
- 验证失败次数

### 指标更新频率

- **实时指标**：每 5 分钟收集一次
- **UI 更新**：30 秒轮询 + WebSocket 推送
- **历史数据**：保留 30 天

## 🛠️ 故障排查

### 常见问题

#### 1. 支付一直失败
**检查：**
```bash
# 查看支付尝试详情
curl https://web-production-fedb.up.railway.app/payments/{ORDER_ID}/attempts \
  -H "Authorization: Bearer YOUR_TOKEN"

# 检查 PSP 状态
curl https://web-production-fedb.up.railway.app/employee/psp/performance \
  -H "Authorization: Bearer EMPLOYEE_TOKEN"
```

**可能原因：**
- 所有 PSP 都失败（检查 PSP 配置）
- 金额超出限额
- 币种不支持
- API 密钥无效

#### 2. 协议验证失败
**检查：**
```bash
# 验证载荷格式
curl -X POST https://web-production-fedb.up.railway.app/protocols/{PROTOCOL}/validate \
  -H "Content-Type: application/json" \
  -d '{"payload": YOUR_PAYLOAD}'
```

**常见错误：**
- 缺少必填字段
- 金额格式错误（必须是数字）
- 币种代码错误（必须是3位ISO代码）
- Items 数组为空（ACP协议）

#### 3. 路由配置不生效
**检查：**
- 路由是否激活（`is_active: true`）
- PSP 名称拼写是否正确
- 优先级数字是否唯一
- Max retries 设置合理（1-5）

## 🚀 高级功能（未来）

### 即将推出

- **成本优化路由**：自动选择最便宜的 PSP
- **性能优化路由**：基于实时表现选择最快的 PSP
- **地理路由**：根据客户位置选择本地 PSP
- **金额分层**：大额小额使用不同路由策略
- **VCC 支持**：虚拟卡支付
- **Stablecoin 支付**：加密货币结算
- **ACH 直连**：银行直接转账

### 预留接口

所有服务都包含 TODO 标记，为未来功能预留接口：
```python
# TODO(phase5): Implement cost-based routing
# TODO(phase5): Add VCC payment support
# TODO(phase5): Integrate stablecoin settlements
```

## 📖 协议规范文档

### AP2 v2.0 规范

**请求格式：**
```typescript
interface AP2PaymentRequest {
  order_id: string;
  amount: number;
  currency: string; // ISO 4217
  merchant_id: string;
  customer?: {
    email: string;
    name: string;
  };
}
```

**响应格式：**
```typescript
interface AP2PaymentResponse {
  transaction_id: string;
  status: 'success' | 'failed' | 'pending';
  order_id: string;
  amount: number;
  currency: string;
  psp_used: string;
  created_at: string;
  protocol: {
    name: 'AP2';
    version: '2.0';
  };
}
```

### ACP v1.0 规范

**请求格式：**
```typescript
interface ACPOrderRequest {
  agent_id: string;
  merchant_id: string;
  items: Array<{
    sku: string;
    name?: string;
    quantity: number;
    price: number;
  }>;
  customer: {
    email: string;
    name: string;
    address?: Address;
  };
  shipping?: {
    method: string;
    cost: number;
  };
}
```

### X-402 v3.1 规范

**请求格式：**
```typescript
interface X402AuthRequest {
  transaction_id: string;
  amount: number;
  currency: string;
  authorization_code: string;
  capture_mode: 'immediate' | 'manual';
  currencies?: string[]; // 多币种支持
}
```

## ✅ 成功案例

### 案例 1: 高可用支付

**场景：** Stripe 临时故障

```
请求 → Stripe (失败，响应时间: timeout)
     → Adyen (成功，响应时间: 450ms)
结果：支付成功完成，用户无感知
```

### 案例 2: 协议兼容性

**场景：** Agent 使用不同协议

```
Agent A 使用 AP2 → 标准支付流程
Agent B 使用 ACP → 完整电商流程
Agent C 使用 X-402 → 多币种高级功能
结果：所有 Agent 都能无缝集成
```

## 🎓 最佳实践

### 1. 路由配置
- ✅ 设置至少 2 个备用 PSP
- ✅ 将最可靠的 PSP 设为优先级 1
- ✅ Max retries 建议设为 2-3
- ✅ Timeout 根据业务需求设置（30-60秒）

### 2. 协议选择
- **AP2**: 简单支付场景
- **ACP**: 完整电商订单
- **X-402**: 需要多币种或高级功能

### 3. 监控
- ✅ 定期检查 PSP 性能指标
- ✅ 关注故障转移频率
- ✅ 订阅关键告警
- ✅ 定期优化路由配置

---

## 🎉 Phase 4 完整功能已就绪！

所有组件都已部署并验证通过：
- ✅ 智能支付路由
- ✅ 自动故障转移
- ✅ 协议支持系统
- ✅ 实时性能监控
- ✅ Employee Portal 集成

**开始使用 Phase 4 功能，提升支付系统的可靠性和灵活性！** 🚀
