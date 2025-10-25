# Orders 端点使用情况总结

## 端点映射

### 1. Agent API (公开，需要 x-api-key)
**前缀**: `/agent/v1`

| 端点 | 完整路径 | 方法 | 用途 | 文件 |
|------|---------|------|------|------|
| `/orders/create` | `/agent/v1/orders/create` | POST | Agent 创建订单 | agent_api.py |
| `/orders/{order_id}` | `/agent/v1/orders/{order_id}` | GET | 获取单个订单 | agent_api.py |
| `/orders` | `/agent/v1/orders` | GET | 列出 Agent 的订单 | agent_api.py, agent_sdk_fixed.py |

**认证**: `x-api-key` header
**用于**: Agent Portal, SDK, MCP Server

---

### 2. Dashboard API (内部，需要 JWT)
**前缀**: `/api/dashboard`

| 端点 | 完整路径 | 方法 | 用途 | 文件 |
|------|---------|------|------|------|
| `/orders` | `/api/dashboard/orders` | GET | 管理员查看所有订单 | dashboard_api.py |
| `/orders/{order_id}` | `/api/dashboard/orders/{order_id}` | GET | 获取订单详情 | dashboard_api.py |

**认证**: JWT token (admin/employee)
**用于**: Employee Portal Dashboard

---

### 3. Orders Management (内部管理)
**前缀**: `/orders`

| 端点 | 完整路径 | 方法 | 用途 | 文件 |
|------|---------|------|------|------|
| `/create` | `/orders/create` | POST | 创建订单（内部） | order_routes.py |
| `/merchant/{merchant_id}` | `/orders/merchant/{merchant_id}` | GET | 商户的订单 | order_routes.py |
| `/{order_id}` | `/orders/{order_id}` | GET | 订单详情 | order_routes.py |
| `/{order_id}/cancel` | `/orders/{order_id}/cancel` | POST | 取消订单 | order_routes.py |
| `/{order_id}/ship` | `/orders/{order_id}/ship` | POST | 发货 | order_routes.py |

**认证**: JWT token (admin)
**用于**: 内部管理、Merchant Portal

---

### 4. MCP Routes (MCP 服务器专用)
**前缀**: `/mcp`

| 端点 | 完整路径 | 方法 | 用途 | 文件 |
|------|---------|------|------|------|
| `/orders` | `/mcp/orders` | POST | MCP 创建订单 | mcp_routes.py |
| `/orders/{order_id}` | `/mcp/orders/{order_id}` | GET | 获取订单 | mcp_routes.py |
| `/orders/agent/{agent_id}` | `/mcp/orders/agent/{agent_id}` | GET | Agent 的订单 | mcp_routes.py |
| `/orders/merchant/{merchant_id}` | `/mcp/orders/merchant/{merchant_id}` | GET | 商户的订单 | mcp_routes.py |
| `/orders/summary` | `/mcp/orders/summary` | GET | 订单统计 | mcp_routes.py |

**认证**: 特殊 MCP 认证
**用于**: MCP Server, Claude Desktop

---

### 5. Payment Routes
**前缀**: `/api/payments`

| 端点 | 完整路径 | 方法 | 用途 | 文件 |
|------|---------|------|------|------|
| `/orders/{order_id}` | `/api/payments/orders/{order_id}` | GET | 支付相关订单信息 | payment_routes.py |

**认证**: JWT token
**用于**: 支付处理流程

---

### 6. Fulfillment API
**前缀**: `/agent/v1/fulfillment`

| 端点 | 完整路径 | 方法 | 用途 | 文件 |
|------|---------|------|------|------|
| `/orders/in-transit` | `/agent/v1/fulfillment/orders/in-transit` | GET | 运输中的订单 | fulfillment_api.py |

**认证**: `x-api-key`
**用于**: Agent Portal 物流追踪

---

## 前端调用映射

### Agent Portal
- **Orders 页面**: `/agent/v1/orders` (已修复✅)
- **Dashboard**: `/agent/v1/orders` (用于统计)
- **Fulfillment**: `/agent/v1/fulfillment/orders/in-transit`

### Employee Portal
- **Dashboard**: `/api/dashboard/orders`
- **Orders 管理**: `/api/dashboard/orders/{order_id}`

### Merchant Portal
- **Orders 页面**: `/orders/merchant/{merchant_id}`
- **订单详情**: `/orders/{order_id}`

---

## 建议的整合方案

### 当前问题
1. 端点分散在多个文件中
2. 功能重复（如 `/agent/v1/orders` 在两个文件中）
3. 命名不一致

### 推荐结构
```
/agent/v1/orders/*          → Agent 专用 (公开 API)
/api/dashboard/orders/*     → Dashboard 专用 (内部)
/orders/*                   → Admin/Internal 管理
/mcp/orders/*              → MCP Server 专用
```

### 下一步行动
1. ✅ 已修复：Agent Portal 使用 `/agent/v1/orders`
2. ⏳ 待办：合并 `agent_sdk_fixed.py` 的重复端点
3. ⏳ 待办：统一认证方式
4. ⏳ 待办：添加端点文档

---

## 检查清单

- [x] Agent Portal Orders 页面正常工作
- [ ] Employee Portal Orders 页面测试
- [ ] Merchant Portal Orders 页面测试
- [ ] SDK orders 方法测试
- [ ] MCP Server orders 工具测试

---

**更新时间**: 2024-10-24
**状态**: Agent Portal 已修复并部署 ✅

