# Agent Routing 设置指南

## 🎯 为什么 Agent 需要路由策略？

路由策略让 Agent 可以：
1. **优化性能** - 选择响应最快的 PSP
2. **降低成本** - 选择费用最低的 PSP  
3. **提高成功率** - 基于历史数据选择最可靠的 PSP

## 两种设置方式

### 方式 1: Employee Portal 快速设置（推荐）⭐

**适用场景**: Employee 为 Agent 配置路由策略

**步骤**:

1. 访问 https://employee.pivota.cc/dashboard/agents
2. 点击 Agent 的 "View" 按钮
3. 展开 **"Routing Policy Configuration"** 部分（紫色 Phase 4++ 徽章）
4. 如果显示 "No Routing Policy Set"，点击 **"Quick Setup Default Policy"** 按钮
5. ✅ 完成！默认策略已创建

**默认策略内容**:
```json
{
  "prefer": ["stripe", "adyen", "paypal"],
  "weights": {
    "stripe": 1.0,    // 100% 优先
    "adyen": 0.9,     // 90% 优先
    "paypal": 0.7     // 70% 优先
  },
  "exclude": [],
  "failover": ["square"]
}
```

**自定义**:
- 点击 PSP 按钮添加到排除列表
- 拖动偏好列表调整顺序
- 调整权重滑块
- 点击 "Save Policy" 保存

### 方式 2: Agent API 端点（开发者使用）

**适用场景**: Agent 通过 API 自主管理路由策略

#### 创建路由策略

```bash
POST /agents/{agent_id}/routing/policies
Authorization: Bearer {AGENT_TOKEN}
Content-Type: application/json

{
  "prefer": ["stripe", "adyen"],
  "weights": {"stripe": 1.0, "adyen": 0.85},
  "exclude": [],
  "failover": ["paypal"],
  "priority": 1
}
```

#### 查看当前策略

```bash
GET /agents/{agent_id}/routing/policies
Authorization: Bearer {AGENT_TOKEN}
```

#### 测试路由

```bash
POST /agents/{agent_id}/routing/test
Authorization: Bearer {AGENT_TOKEN}
Content-Type: application/json

{
  "merchant_id": "merchant_123",
  "amount": 100.00,
  "currency": "USD"
}
```

## 策略配置最佳实践

### 1. 性能优先型

```json
{
  "prefer": ["stripe", "adyen", "paypal"],
  "weights": {
    "stripe": 1.0,   // Stripe 最快
    "adyen": 0.85,   // Adyen 次之
    "paypal": 0.6    // PayPal 较慢
  }
}
```

**适合**: 对响应时间敏感的应用

### 2. 成本优先型

```json
{
  "prefer": ["paypal", "square", "stripe"],
  "weights": {
    "paypal": 1.0,   // PayPal 费用最低
    "square": 0.9,   
    "stripe": 0.7    // Stripe 费用较高
  }
}
```

**适合**: 高频低额交易

### 3. 可靠性优先型

```json
{
  "prefer": ["stripe", "adyen"],
  "weights": {
    "stripe": 1.0,   // 99.9% 成功率
    "adyen": 0.95    // 99.5% 成功率
  },
  "exclude": ["square"]  // 排除不稳定的 PSP
}
```

**适合**: 高价值交易

## 路由策略 vs 收益策略

### 路由策略（Routing Policy）
- **作用**: 决定选择哪个 PSP
- **设置位置**: "Routing Policy Configuration" 部分
- **API**: `/agents/{id}/routing/policies`

### 收益策略（Revenue Policy）
- **作用**: 决定 Agent 从每笔交易中获得多少分成
- **设置位置**: "Revenue & Earnings" 部分（Employee 创建）
- **API**: `/agents/{id}/revenue/policies`

## 查看路由效果

### Agent Routing Decisions 部分

展开 **"Agent Routing Decisions"**（绿色 Phase 5 徽章）可以看到：

- **双路径可视化**:
  - 左侧：商户路径（蓝色）
  - 中间：最终决策
  - 右侧：Agent 路径（绿色）

- **颜色含义**:
  - 🔵 蓝色徽章 = 商户规则生效
  - 🟢 绿色徽章 = Agent 规则生效
  - 🟣 紫色徽章 = 双方共识

## 权限说明

### Employee 可以:
✅ 查看所有 Agent 的路由策略  
✅ 为 Agent 创建路由策略  
✅ 快速设置默认策略  
✅ 创建收益分成策略  
✅ 启用/禁用 Agent 的收益分成

### Agent 可以:
✅ 查看自己的路由策略  
✅ 创建/更新自己的路由策略  
✅ 测试路由决策（模拟）  
✅ 查看路由历史  
✅ 查看收益摘要  
❌ 不能修改收益分成比例（只有 Employee 可以）

## 常见问题

### Q: Agent 还没有路由策略怎么办？

**A**: 在 Agent 详情页展开 "Routing Policy Configuration"，点击 "Quick Setup Default Policy" 按钮即可一键创建。

### Q: 如何知道路由策略是否生效？

**A**: 
1. 展开 "Agent Routing Decisions" 查看历史记录
2. 使用 `/agents/{id}/routing/test` 端点进行模拟测试
3. 查看实际支付的路由日志

### Q: Agent 可以覆盖商户的规则吗？

**A**: 
- 默认情况下：**不可以**，商户规则优先（安全第一）
- 白名单模式：Employee 可以为特定 Agent 启用覆盖权限

### Q: 如何启用收益分成？

**A**: Employee 需要：
1. 在 "Revenue & Earnings" 部分创建收益策略
2. 在 Agent 设置中启用 `revenue_sharing_enabled`

## 快速开始示例

### 为新 Agent 配置完整策略

```bash
# 1. 创建路由策略
curl -X POST "$API/employee/routing/policies/agent/{agent_id}" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "prefer": ["stripe", "adyen"],
    "weights": {"stripe": 1.0, "adyen": 0.9},
    "exclude": [],
    "failover": ["paypal"],
    "priority": 1
  }'

# 2. 创建收益策略（2% 分成）
curl -X POST "$API/agents/{agent_id}/revenue/policies" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "merchant_id": null,
    "split_ratio": 0.02,
    "currency": "USD",
    "min_transaction_amount": 10.00
  }'

# 3. 启用收益分成
curl -X PUT "$API/employee/agents/{agent_id}/revenue-sharing?enabled=true" \
  -H "Authorization: Bearer $TOKEN"
```

## 下一步

配置完路由策略后：
1. 进行测试支付，查看路由决策
2. 监控 "Agent Routing Decisions" 中的历史记录
3. 根据实际表现调整策略
4. 查看 "Revenue & Earnings" 中的收益累积

---

**现在 Employee 可以轻松为 Agent 配置路由策略了！** 🚀

## 🎯 为什么 Agent 需要路由策略？

路由策略让 Agent 可以：
1. **优化性能** - 选择响应最快的 PSP
2. **降低成本** - 选择费用最低的 PSP  
3. **提高成功率** - 基于历史数据选择最可靠的 PSP

## 两种设置方式

### 方式 1: Employee Portal 快速设置（推荐）⭐

**适用场景**: Employee 为 Agent 配置路由策略

**步骤**:

1. 访问 https://employee.pivota.cc/dashboard/agents
2. 点击 Agent 的 "View" 按钮
3. 展开 **"Routing Policy Configuration"** 部分（紫色 Phase 4++ 徽章）
4. 如果显示 "No Routing Policy Set"，点击 **"Quick Setup Default Policy"** 按钮
5. ✅ 完成！默认策略已创建

**默认策略内容**:
```json
{
  "prefer": ["stripe", "adyen", "paypal"],
  "weights": {
    "stripe": 1.0,    // 100% 优先
    "adyen": 0.9,     // 90% 优先
    "paypal": 0.7     // 70% 优先
  },
  "exclude": [],
  "failover": ["square"]
}
```

**自定义**:
- 点击 PSP 按钮添加到排除列表
- 拖动偏好列表调整顺序
- 调整权重滑块
- 点击 "Save Policy" 保存

### 方式 2: Agent API 端点（开发者使用）

**适用场景**: Agent 通过 API 自主管理路由策略

#### 创建路由策略

```bash
POST /agents/{agent_id}/routing/policies
Authorization: Bearer {AGENT_TOKEN}
Content-Type: application/json

{
  "prefer": ["stripe", "adyen"],
  "weights": {"stripe": 1.0, "adyen": 0.85},
  "exclude": [],
  "failover": ["paypal"],
  "priority": 1
}
```

#### 查看当前策略

```bash
GET /agents/{agent_id}/routing/policies
Authorization: Bearer {AGENT_TOKEN}
```

#### 测试路由

```bash
POST /agents/{agent_id}/routing/test
Authorization: Bearer {AGENT_TOKEN}
Content-Type: application/json

{
  "merchant_id": "merchant_123",
  "amount": 100.00,
  "currency": "USD"
}
```

## 策略配置最佳实践

### 1. 性能优先型

```json
{
  "prefer": ["stripe", "adyen", "paypal"],
  "weights": {
    "stripe": 1.0,   // Stripe 最快
    "adyen": 0.85,   // Adyen 次之
    "paypal": 0.6    // PayPal 较慢
  }
}
```

**适合**: 对响应时间敏感的应用

### 2. 成本优先型

```json
{
  "prefer": ["paypal", "square", "stripe"],
  "weights": {
    "paypal": 1.0,   // PayPal 费用最低
    "square": 0.9,   
    "stripe": 0.7    // Stripe 费用较高
  }
}
```

**适合**: 高频低额交易

### 3. 可靠性优先型

```json
{
  "prefer": ["stripe", "adyen"],
  "weights": {
    "stripe": 1.0,   // 99.9% 成功率
    "adyen": 0.95    // 99.5% 成功率
  },
  "exclude": ["square"]  // 排除不稳定的 PSP
}
```

**适合**: 高价值交易

## 路由策略 vs 收益策略

### 路由策略（Routing Policy）
- **作用**: 决定选择哪个 PSP
- **设置位置**: "Routing Policy Configuration" 部分
- **API**: `/agents/{id}/routing/policies`

### 收益策略（Revenue Policy）
- **作用**: 决定 Agent 从每笔交易中获得多少分成
- **设置位置**: "Revenue & Earnings" 部分（Employee 创建）
- **API**: `/agents/{id}/revenue/policies`

## 查看路由效果

### Agent Routing Decisions 部分

展开 **"Agent Routing Decisions"**（绿色 Phase 5 徽章）可以看到：

- **双路径可视化**:
  - 左侧：商户路径（蓝色）
  - 中间：最终决策
  - 右侧：Agent 路径（绿色）

- **颜色含义**:
  - 🔵 蓝色徽章 = 商户规则生效
  - 🟢 绿色徽章 = Agent 规则生效
  - 🟣 紫色徽章 = 双方共识

## 权限说明

### Employee 可以:
✅ 查看所有 Agent 的路由策略  
✅ 为 Agent 创建路由策略  
✅ 快速设置默认策略  
✅ 创建收益分成策略  
✅ 启用/禁用 Agent 的收益分成

### Agent 可以:
✅ 查看自己的路由策略  
✅ 创建/更新自己的路由策略  
✅ 测试路由决策（模拟）  
✅ 查看路由历史  
✅ 查看收益摘要  
❌ 不能修改收益分成比例（只有 Employee 可以）

## 常见问题

### Q: Agent 还没有路由策略怎么办？

**A**: 在 Agent 详情页展开 "Routing Policy Configuration"，点击 "Quick Setup Default Policy" 按钮即可一键创建。

### Q: 如何知道路由策略是否生效？

**A**: 
1. 展开 "Agent Routing Decisions" 查看历史记录
2. 使用 `/agents/{id}/routing/test` 端点进行模拟测试
3. 查看实际支付的路由日志

### Q: Agent 可以覆盖商户的规则吗？

**A**: 
- 默认情况下：**不可以**，商户规则优先（安全第一）
- 白名单模式：Employee 可以为特定 Agent 启用覆盖权限

### Q: 如何启用收益分成？

**A**: Employee 需要：
1. 在 "Revenue & Earnings" 部分创建收益策略
2. 在 Agent 设置中启用 `revenue_sharing_enabled`

## 快速开始示例

### 为新 Agent 配置完整策略

```bash
# 1. 创建路由策略
curl -X POST "$API/employee/routing/policies/agent/{agent_id}" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "prefer": ["stripe", "adyen"],
    "weights": {"stripe": 1.0, "adyen": 0.9},
    "exclude": [],
    "failover": ["paypal"],
    "priority": 1
  }'

# 2. 创建收益策略（2% 分成）
curl -X POST "$API/agents/{agent_id}/revenue/policies" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "merchant_id": null,
    "split_ratio": 0.02,
    "currency": "USD",
    "min_transaction_amount": 10.00
  }'

# 3. 启用收益分成
curl -X PUT "$API/employee/agents/{agent_id}/revenue-sharing?enabled=true" \
  -H "Authorization: Bearer $TOKEN"
```

## 下一步

配置完路由策略后：
1. 进行测试支付，查看路由决策
2. 监控 "Agent Routing Decisions" 中的历史记录
3. 根据实际表现调整策略
4. 查看 "Revenue & Earnings" 中的收益累积

---

**现在 Employee 可以轻松为 Agent 配置路由策略了！** 🚀
