# Employee Agents Module - 快速参考

## 🎯 核心功能概览

### 三大核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                     Agents Management Page                   │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 📊 Summary Stats (4 cards)                          │    │
│  │ • Total Agents    • 24h Requests                     │    │
│  │ • Total GMV       • Avg Success Rate                 │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 🔍 Search & Filters                                 │    │
│  │ [搜索框] [状态过滤]                                  │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 📋 AgentTable (主列表)                              │    │
│  │ ┌────────┬────────┬────────┬────────┬──────────┐   │    │
│  │ │ Agent  │ Status │API Key │ Rate   │ Metrics  │   │    │
│  │ ├────────┼────────┼────────┼────────┼──────────┤   │    │
│  │ │ Alice  │ Active │ ak_... │ 100/m  │ 1.2k req │   │    │
│  │ │ Bob    │ Active │ ak_... │ 200/m  │ 856 req  │   │    │
│  │ └────────┴────────┴────────┴────────┴──────────┘   │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                               │
│  [点击 Agent] ────────────────────────────────┐              │
│                                                ▼              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 🔍 AgentDetailPanel (Modal)                          │   │
│  │                                                       │   │
│  │  📌 Basic Info                                        │   │
│  │  🔑 API Key Management (show/hide/copy/reset)        │   │
│  │  📊 Performance Metrics (24h)                         │   │
│  │  ⚙️  Governance (rate limit editor)                   │   │
│  │  [Stop/Activate]                                      │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 📜 AgentCallsTable (当 agent 被选中时)              │    │
│  │ ┌────────┬────────┬────────┬────────┬──────────┐   │    │
│  │ │ Time   │ Method │Endpoint│ Status │ Latency  │   │    │
│  │ ├────────┼────────┼────────┼────────┼──────────┤   │    │
│  │ │ 10:23  │ POST   │/orders │  200   │  45ms    │   │    │
│  │ │ 10:22  │ GET    │/products│ 200   │  12ms    │   │    │
│  │ └────────┴────────┴────────┴────────┴──────────┘   │    │
│  │ [Prev] Page 1/5 [Next]                              │   │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## 🗂️ 文件清单

### 前端文件 (5个)
```
pivota-employee-portal/
├── app/
│   ├── components/agents/
│   │   ├── AgentTable.tsx          ✅ NEW (257行)
│   │   ├── AgentDetailPanel.tsx    ✅ NEW (343行)
│   │   └── AgentCallsTable.tsx     ✅ NEW (275行)
│   └── dashboard/agents/
│       └── page.tsx                ✅ REWRITTEN (316行)
└── lib/
    └── api-client.ts               ✅ UPDATED (+40行)
```

### 后端文件 (2个)
```
pivota_infra/
├── routes/
│   └── employee_agents_management.py  ✅ NEW (495行)
└── database/migrations/
    └── 007_agent_metrics.sql          ✅ NEW
```

## 🔌 API 端点速查

### Base URL
```
https://web-production-fedb.up.railway.app
```

### 端点列表
| 方法 | 端点 | 功能 | 参数 |
|------|------|------|------|
| GET | `/employee/agents` | 列表 | `?status_filter=active` |
| GET | `/employee/agents/{id}/details` | 详情 | - |
| GET | `/employee/agents/{id}/calls` | 日志 | `?limit=50&offset=0` |
| POST | `/employee/agents/{id}/reset-api-key` | 重置Key | - |
| POST | `/employee/agents/{id}/update-rate-limit` | 更新限制 | `?new_limit=200` |
| POST | `/employee/agents/{id}/deactivate` | 停用 | `{"reason":"..."}` |
| POST | `/employee/agents/{id}/reactivate` | 激活 | - |

### 示例调用
```bash
# 获取所有 agents
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/employee/agents

# 获取详情
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/employee/agents/agent_123/details

# 更新 rate limit
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "https://web-production-fedb.up.railway.app/employee/agents/agent_123/update-rate-limit?new_limit=300"
```

## 📊 数据结构

### Agent 对象
```typescript
interface Agent {
  // 基本信息
  agent_id: string
  name: string
  email: string
  company?: string
  use_case?: string
  status: 'active' | 'inactive' | 'suspended'
  
  // API 管理
  api_key: string
  rate_limit: number
  created_at: string
  last_active?: string
  
  // 业务数据
  merchant_count: number
  
  // 性能指标 (24h)
  metrics: {
    requests_24h: number
    successful_24h: number
    failed_24h: number
    success_rate: number      // 0-100
    avg_latency_ms: number
    total_gmv: number
    total_orders: number
  }
  
  // 治理策略
  governance: {
    max_error_rate: number    // 0-1
    max_requests_per_minute: number
    policy_status: string
    last_violation?: string
  }
}
```

### AgentCall 对象
```typescript
interface AgentCall {
  id: number
  endpoint: string
  method: 'GET' | 'POST' | 'PUT' | 'DELETE'
  merchant_id?: string
  status_code: number
  response_time_ms: number
  error_message?: string
  order_id?: string
  order_amount?: number
  timestamp: string
}
```

## 🎨 UI 颜色规范

### 状态颜色
```css
Active:    bg-green-100 text-green-700
Inactive:  bg-gray-100  text-gray-700
Suspended: bg-red-100   text-red-700
```

### 成功率颜色
```typescript
≥ 98%  → text-green-600   // 优秀
≥ 95%  → text-yellow-600  // 良好
< 95%  → text-red-600     // 需要关注
```

### HTTP 方法颜色
```css
GET:    bg-blue-100   text-blue-700
POST:   bg-green-100  text-green-700
PUT:    bg-yellow-100 text-yellow-700
DELETE: bg-red-100    text-red-700
```

### 区块颜色
```css
基本信息:  bg-gray-50
API管理:   bg-blue-50
性能指标:  bg-green-50
治理策略:  bg-yellow-50
```

## ⚡ 关键功能快捷方式

### 常用操作流程

#### 1️⃣ 查看 Agent 性能
```
点击 Agent → 查看 Metrics 卡片 → 24h 数据一目了然
```

#### 2️⃣ 重置 API Key
```
点击 Agent → API Key 管理 → 点击"重置" → 确认 → 复制新 Key
```

#### 3️⃣ 调整 Rate Limit
```
点击 Agent → 治理策略 → 点击"编辑" → 输入新值 → 保存
```

#### 4️⃣ 查看调用日志
```
点击 Agent → 自动显示调用日志 → 使用过滤器/分页查看
```

#### 5️⃣ 导出日志
```
选中 Agent → 调用日志表 → 点击导出图标 → 下载 CSV
```

#### 6️⃣ 停用问题 Agent
```
点击 Agent → 底部"停用Agent" → 确认 → 完成
```

## 🔍 搜索和过滤

### 搜索字段
- Agent 名称
- Email
- 公司名称

### 过滤选项
- 全部状态
- 活跃 (active)
- 停用 (inactive)
- 暂停 (suspended)

### 调用日志过滤
- 全部请求
- 成功请求 (2xx)
- 错误请求 (4xx, 5xx)

## 📱 响应式断点

```css
Mobile:  < 640px   (单列布局)
Tablet:  640-1024px (2列布局)
Desktop: > 1024px  (4列布局)
```

## 🚨 重要提示

### ⚠️ 谨慎操作
- **重置 API Key**: 会立即使旧 key 失效
- **停用 Agent**: 会阻止所有 API 调用
- **修改 Rate Limit**: 立即生效

### ✅ 安全机制
- 所有敏感操作都需要确认
- API Key 默认遮罩显示
- 所有操作都有审计日志
- 需要 Employee/Admin 角色

## 🐛 常见问题

### Q: Metrics 显示为 0？
**A**: 可能是新 agent 或数据未同步，等待几分钟或刷新页面。

### Q: 无法重置 API Key？
**A**: 检查权限，确保是 employee 或 admin 角色。

### Q: 调用日志为空？
**A**: Agent 可能还没有 API 调用，或日志数据还未写入。

### Q: Rate Limit 修改不生效？
**A**: 刷新页面，检查是否有错误提示。

## 📞 技术支持

### 相关文档
- 完整文档: `EMPLOYEE_AGENTS_MODULE_COMPLETE.md`
- 部署指南: `EMPLOYEE_AGENTS_DEPLOYMENT.md`
- API 测试: `test_employee_agents_module.sh`

### 联系方式
- 技术问题: 查看后端日志
- UI 问题: 查看浏览器 console
- 数据问题: 检查数据库表

---

**快速参考版本**: 1.0  
**更新日期**: 2025-11-01  
**适用于**: Employee Portal v2.0+

