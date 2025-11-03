# Phase 5: Agent Routing Control + Revenue Layer - 用户指南

## 🚀 快速开始

### 1. 运行数据库迁移（部署后）

```bash
# Migration 012a: 收益表
curl -X POST "https://web-production-fedb.up.railway.app/admin/migrations/run-012a-agent-revenue" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN"

# Migration 012b: 路由扩展
curl -X POST "https://web-production-fedb.up.railway.app/admin/migrations/run-012b-routing-extensions" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN"
```

### 2. 访问新功能

**Employee Portal - Agent 详情页**:
1. 打开 https://employee.pivota.cc/dashboard/agents
2. 点击任意 Agent 的 "View" 按钮
3. 查看新的 Phase 5 部分（绿色徽章）

## 核心功能

### 1. Agent Routing Decisions（代理路由决策）

**功能**: 双路径可视化展示路由决策过程

**特性**:
- 🔵 蓝色路径 = 商户规则生效
- 🟢 绿色路径 = 代理规则生效
- 🟣 紫色 = 双方共识
- 冲突自动检测和高亮

**访问**: Agent 详情页 → "Agent Routing Decisions" 部分

### 2. Revenue & Earnings（收益和盈利）

**功能**: 代理收益追踪和管理

**显示内容**:
- 24小时收益
- 7天收益
- 30天收益
- 结算状态（已结算 vs 待结算）
- 收益分成政策

**访问**: Agent 详情页 → "Revenue & Earnings" 部分

## API 使用指南

### Agent Routing API

#### 测试路由
```bash
POST /agents/{agent_id}/routing/test
{
  "merchant_id": "merchant_123",
  "amount": 100.00,
  "currency": "USD"
}
```

**返回**: 模拟的路由决策（不实际执行）

#### 获取路由历史
```bash
GET /agents/{agent_id}/routing/history?days=30&limit=50
```

**返回**: 最近30天的路由决策记录

### Agent Revenue API

#### 创建收益政策
```bash
POST /agents/{agent_id}/revenue/policies
{
  "merchant_id": null,           # null = 默认（所有商户）
  "split_ratio": 0.02,           # 2%
  "currency": "USD",
  "min_transaction_amount": 10.00
}
```

#### 查看收益摘要
```bash
GET /agents/{agent_id}/revenue/earnings?days=30&currency=USD
```

**返回**:
```json
{
  "total_earned": 50.00,
  "settled_amount": 30.00,
  "pending_amount": 20.00,
  "total_transactions": 100,
  "avg_split_ratio": 0.02,
  "currency": "USD",
  "period_days": 30
}
```

#### 查看收益日志
```bash
GET /agents/{agent_id}/revenue/logs?days=7
```

**返回**: 每笔交易的收益明细

#### 查看结算历史
```bash
GET /agents/{agent_id}/revenue/settlements
```

**返回**: 结算批次历史

## 收益政策配置

### 政策类型

#### 1. 默认政策（推荐）
```json
{
  "merchant_id": null,
  "split_ratio": 0.01,  // 1%
  "currency": "USD",
  "min_transaction_amount": 0
}
```

适用于所有商户，最简单的配置。

#### 2. 商户特定政策
```json
{
  "merchant_id": "merchant_vip_001",
  "split_ratio": 0.03,  // 3%（VIP商户更高分成）
  "currency": "USD",
  "min_transaction_amount": 100.00
}
```

为特定商户设置不同的分成比例。

#### 3. 金额范围政策
```json
{
  "merchant_id": null,
  "split_ratio": 0.025,  // 2.5%
  "currency": "USD",
  "min_transaction_amount": 1000.00,  // 大额交易
  "max_transaction_amount": 10000.00
}
```

只对特定金额范围生效。

### 政策优先级

1. **商户特定 + 金额范围匹配** - 最高优先级
2. **商户特定** - 中优先级
3. **默认政策** - 最低优先级
4. **无收益** - 如果没有匹配的政策

## 路由决策流程

```
1. Agent 发起支付请求
   ↓
2. AgentRoutingController 协调
   ↓
3. DualRoutingEngine.route_payment()
   - evaluate_policy() 评估商户和代理规则
   - detect_conflicts() 检测冲突
   - resolve() 解决并选择 PSP
   ↓
4. 执行支付（通过 PSP 或 AP2 适配器）
   ↓
5. 记录路由决策（routing_logs）
   ↓
6. 计算收益分成（如果启用）
   ↓
7. 记录收益（agent_revenue_logs）
```

## 收益结算流程

### 自动结算任务

#### 每日任务（00:00 UTC）
- 计算待结算收益
- 生成每日报告

#### 每周任务（周一 00:00 UTC）
- 创建结算批次
- 标记为 "processing"
- 生成结算单

#### 每月任务（1号 00:00 UTC）
- 更新收益分析
- 生成月度报告

### 结算状态

- `pending` - 待结算（刚创建）
- `processing` - 处理中（已加入批次）
- `settled` - 已结算（已支付）
- `failed` - 失败
- `cancelled` - 取消

## 高级特性

### 1. 干运行模拟

使用 `DualRoutingEngine.simulate()` 方法可以无风险测试策略：

```python
result = engine.simulate(context, dry_run=True)
# 返回完整决策，但不写入数据库
```

### 2. 策略评估

使用 `evaluate_policy()` 可以单独测试策略合并逻辑：

```python
evaluation = engine.evaluate_policy(merchant_rules, agent_rules)
# 返回冲突检测和合并结果，无需完整路由
```

### 3. 灵活的日志控制

```python
result = engine.route_payment(context)
log_id = engine.log_decision(result, persist=True)  # 或 False 跳过
```

## 监控和分析

### Employee Portal 监控

1. **Routing Management 页面**:
   - 查看所有路由决策
   - 分析 PSP 使用分布
   - 监控冲突率

2. **Agent 详情页**:
   - 查看该 Agent 的路由历史
   - 监控收益累积
   - 查看结算状态

### 关键指标

- **路由成功率**: 选中的 PSP 是否可用
- **冲突率**: 商户和代理规则的冲突频率
- **平均分成比例**: Agent 的平均收益率
- **结算周期**: 从待结算到已结算的时间

## 故障排查

### 问题: 收益为 0

**可能原因**:
1. 没有配置收益政策
2. 交易金额不在政策范围内
3. 没有实际交易发生

**解决**:
- 检查收益政策: `GET /agents/{id}/revenue/policies`
- 确认交易金额符合 `min_transaction_amount`
- 查看收益日志: `GET /agents/{id}/revenue/logs`

### 问题: 路由历史为空

**可能原因**:
1. 还没有通过该 Agent 执行过支付
2. 时间范围太短

**解决**:
- 扩大查询范围: `?days=90`
- 执行测试支付
- 使用 `/routing/test` 端点进行模拟

## 最佳实践

### 1. 收益政策配置

- 先设置默认政策（merchant_id = NULL）
- 为 VIP 商户设置特殊政策
- 定期审查和调整分成比例

### 2. 路由策略

- Agent 策略应该补充而非对抗商户策略
- 使用权重优化 PSP 选择
- 定期测试策略变更（使用 `/routing/test`）

### 3. 监控

- 每周检查收益累积
- 监控结算状态
- 分析路由决策模式

## 下一步（Phase 6准备）

Phase 5 为 Agent Portal 自助服务打下基础。未来 Agent Portal 将包括：

- ✅ Agent 自主管理路由策略
- ✅ 实时查看收益
- ✅ 下载结算报告
- ✅ 路由决策透明化
- ✅ 性能分析仪表板

---

**Phase 5 已完全就绪！Agent 现在可以控制路由并获得收益分成。** 🎉

## 🚀 快速开始

### 1. 运行数据库迁移（部署后）

```bash
# Migration 012a: 收益表
curl -X POST "https://web-production-fedb.up.railway.app/admin/migrations/run-012a-agent-revenue" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN"

# Migration 012b: 路由扩展
curl -X POST "https://web-production-fedb.up.railway.app/admin/migrations/run-012b-routing-extensions" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN"
```

### 2. 访问新功能

**Employee Portal - Agent 详情页**:
1. 打开 https://employee.pivota.cc/dashboard/agents
2. 点击任意 Agent 的 "View" 按钮
3. 查看新的 Phase 5 部分（绿色徽章）

## 核心功能

### 1. Agent Routing Decisions（代理路由决策）

**功能**: 双路径可视化展示路由决策过程

**特性**:
- 🔵 蓝色路径 = 商户规则生效
- 🟢 绿色路径 = 代理规则生效
- 🟣 紫色 = 双方共识
- 冲突自动检测和高亮

**访问**: Agent 详情页 → "Agent Routing Decisions" 部分

### 2. Revenue & Earnings（收益和盈利）

**功能**: 代理收益追踪和管理

**显示内容**:
- 24小时收益
- 7天收益
- 30天收益
- 结算状态（已结算 vs 待结算）
- 收益分成政策

**访问**: Agent 详情页 → "Revenue & Earnings" 部分

## API 使用指南

### Agent Routing API

#### 测试路由
```bash
POST /agents/{agent_id}/routing/test
{
  "merchant_id": "merchant_123",
  "amount": 100.00,
  "currency": "USD"
}
```

**返回**: 模拟的路由决策（不实际执行）

#### 获取路由历史
```bash
GET /agents/{agent_id}/routing/history?days=30&limit=50
```

**返回**: 最近30天的路由决策记录

### Agent Revenue API

#### 创建收益政策
```bash
POST /agents/{agent_id}/revenue/policies
{
  "merchant_id": null,           # null = 默认（所有商户）
  "split_ratio": 0.02,           # 2%
  "currency": "USD",
  "min_transaction_amount": 10.00
}
```

#### 查看收益摘要
```bash
GET /agents/{agent_id}/revenue/earnings?days=30&currency=USD
```

**返回**:
```json
{
  "total_earned": 50.00,
  "settled_amount": 30.00,
  "pending_amount": 20.00,
  "total_transactions": 100,
  "avg_split_ratio": 0.02,
  "currency": "USD",
  "period_days": 30
}
```

#### 查看收益日志
```bash
GET /agents/{agent_id}/revenue/logs?days=7
```

**返回**: 每笔交易的收益明细

#### 查看结算历史
```bash
GET /agents/{agent_id}/revenue/settlements
```

**返回**: 结算批次历史

## 收益政策配置

### 政策类型

#### 1. 默认政策（推荐）
```json
{
  "merchant_id": null,
  "split_ratio": 0.01,  // 1%
  "currency": "USD",
  "min_transaction_amount": 0
}
```

适用于所有商户，最简单的配置。

#### 2. 商户特定政策
```json
{
  "merchant_id": "merchant_vip_001",
  "split_ratio": 0.03,  // 3%（VIP商户更高分成）
  "currency": "USD",
  "min_transaction_amount": 100.00
}
```

为特定商户设置不同的分成比例。

#### 3. 金额范围政策
```json
{
  "merchant_id": null,
  "split_ratio": 0.025,  // 2.5%
  "currency": "USD",
  "min_transaction_amount": 1000.00,  // 大额交易
  "max_transaction_amount": 10000.00
}
```

只对特定金额范围生效。

### 政策优先级

1. **商户特定 + 金额范围匹配** - 最高优先级
2. **商户特定** - 中优先级
3. **默认政策** - 最低优先级
4. **无收益** - 如果没有匹配的政策

## 路由决策流程

```
1. Agent 发起支付请求
   ↓
2. AgentRoutingController 协调
   ↓
3. DualRoutingEngine.route_payment()
   - evaluate_policy() 评估商户和代理规则
   - detect_conflicts() 检测冲突
   - resolve() 解决并选择 PSP
   ↓
4. 执行支付（通过 PSP 或 AP2 适配器）
   ↓
5. 记录路由决策（routing_logs）
   ↓
6. 计算收益分成（如果启用）
   ↓
7. 记录收益（agent_revenue_logs）
```

## 收益结算流程

### 自动结算任务

#### 每日任务（00:00 UTC）
- 计算待结算收益
- 生成每日报告

#### 每周任务（周一 00:00 UTC）
- 创建结算批次
- 标记为 "processing"
- 生成结算单

#### 每月任务（1号 00:00 UTC）
- 更新收益分析
- 生成月度报告

### 结算状态

- `pending` - 待结算（刚创建）
- `processing` - 处理中（已加入批次）
- `settled` - 已结算（已支付）
- `failed` - 失败
- `cancelled` - 取消

## 高级特性

### 1. 干运行模拟

使用 `DualRoutingEngine.simulate()` 方法可以无风险测试策略：

```python
result = engine.simulate(context, dry_run=True)
# 返回完整决策，但不写入数据库
```

### 2. 策略评估

使用 `evaluate_policy()` 可以单独测试策略合并逻辑：

```python
evaluation = engine.evaluate_policy(merchant_rules, agent_rules)
# 返回冲突检测和合并结果，无需完整路由
```

### 3. 灵活的日志控制

```python
result = engine.route_payment(context)
log_id = engine.log_decision(result, persist=True)  # 或 False 跳过
```

## 监控和分析

### Employee Portal 监控

1. **Routing Management 页面**:
   - 查看所有路由决策
   - 分析 PSP 使用分布
   - 监控冲突率

2. **Agent 详情页**:
   - 查看该 Agent 的路由历史
   - 监控收益累积
   - 查看结算状态

### 关键指标

- **路由成功率**: 选中的 PSP 是否可用
- **冲突率**: 商户和代理规则的冲突频率
- **平均分成比例**: Agent 的平均收益率
- **结算周期**: 从待结算到已结算的时间

## 故障排查

### 问题: 收益为 0

**可能原因**:
1. 没有配置收益政策
2. 交易金额不在政策范围内
3. 没有实际交易发生

**解决**:
- 检查收益政策: `GET /agents/{id}/revenue/policies`
- 确认交易金额符合 `min_transaction_amount`
- 查看收益日志: `GET /agents/{id}/revenue/logs`

### 问题: 路由历史为空

**可能原因**:
1. 还没有通过该 Agent 执行过支付
2. 时间范围太短

**解决**:
- 扩大查询范围: `?days=90`
- 执行测试支付
- 使用 `/routing/test` 端点进行模拟

## 最佳实践

### 1. 收益政策配置

- 先设置默认政策（merchant_id = NULL）
- 为 VIP 商户设置特殊政策
- 定期审查和调整分成比例

### 2. 路由策略

- Agent 策略应该补充而非对抗商户策略
- 使用权重优化 PSP 选择
- 定期测试策略变更（使用 `/routing/test`）

### 3. 监控

- 每周检查收益累积
- 监控结算状态
- 分析路由决策模式

## 下一步（Phase 6准备）

Phase 5 为 Agent Portal 自助服务打下基础。未来 Agent Portal 将包括：

- ✅ Agent 自主管理路由策略
- ✅ 实时查看收益
- ✅ 下载结算报告
- ✅ 路由决策透明化
- ✅ 性能分析仪表板

---

**Phase 5 已完全就绪！Agent 现在可以控制路由并获得收益分成。** 🎉
