# 填充 Routing Management 页面数据

## 当前状态

✅ **页面已部署**: https://employee.pivota.cc/dashboard/routing  
✅ **SQL 错误已修复**: 刷新后应该不再有红色错误  
⚠️ **三个标签页为空**: 因为还没有路由日志数据

## 为什么是空的？

1. **Routing Trace** - 需要实际的路由决策记录
2. **PSP Analytics** - 需要历史PSP选择数据
3. **Conflict Resolution** - 需要有冲突的路由记录

这些数据会在**实际使用**双向路由时自动生成。

## 快速填充测试数据的方法

### 方法 1: 等待部署后通过 API（推荐）

1. **等待2-3分钟**，让 Railway 部署完成

2. **调用 seed 端点**创建测试数据:
```bash
curl -X POST "https://web-production-fedb.up.railway.app/admin/seed/routing-logs" \
  -H "Authorization: Bearer YOUR_NEW_TOKEN"
```

3. **刷新页面**（Cmd+Shift+R），应该能看到：
   - 5条路由日志
   - 1条冲突记录
   - PSP分布图表

### 方法 2: 通过 Employee Portal UI（更推荐）

1. **访问 Agent 详情页**：
   - https://employee.pivota.cc/dashboard/agents
   - 点击 "View" 打开 agent_ee38f2b3645a2ec2

2. **展开 "Routing Policy Configuration" 部分**

3. **编辑策略并保存**（这会触发路由逻辑）

4. **返回 Routing Management 页面**查看数据

### 方法 3: 通过实际支付（生产数据）

当 Agent 通过系统进行支付时，系统会自动：
- 评估商户和代理的路由策略
- 记录决策过程到 `routing_logs` 表
- 检测并记录任何冲突
- 显示在 Routing Management 页面

## 验证步骤

### Step 1: 刷新页面确认错误消失
```
https://employee.pivota.cc/dashboard/routing
```
- 应该看到 "No routing logs found" 而不是红色错误

### Step 2: 查看策略配置
在 Agent 详情页应该能看到：
- ✅ "Routing Policy Configuration" 部分
- ✅ 可以编辑 PSP 偏好、权重、排除列表

### Step 3: 获取新的 Employee Token

如果需要通过 API 测试，先获取新 token：
```bash
curl -X POST "https://web-production-fedb.up.railway.app/employee/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "employee@pivota.com",
    "password": "admin123"
  }'
```

## 已经创建的策略

✅ **merchant_high_risk_001**: 只允许 Stripe  
✅ **merchant_cost_sensitive_002**: 排除 Adyen，偏好 PayPal  
✅ **agent_ee38f2b3645a2ec2**: 偏好 Stripe > Adyen > PayPal（权重）

这些策略已经存在，**只是缺少实际的路由执行记录**。

## 下一步

1. **刷新页面** - 确认错误已消失
2. **等待部署完成** - 约5分钟（Railway + Vercel）
3. **调用 seed 端点** - 创建测试数据
4. **或直接使用UI** - 通过 Agent 详情页编辑策略

部署完成后，Routing Management 页面将完全功能化！🚀

## 当前状态

✅ **页面已部署**: https://employee.pivota.cc/dashboard/routing  
✅ **SQL 错误已修复**: 刷新后应该不再有红色错误  
⚠️ **三个标签页为空**: 因为还没有路由日志数据

## 为什么是空的？

1. **Routing Trace** - 需要实际的路由决策记录
2. **PSP Analytics** - 需要历史PSP选择数据
3. **Conflict Resolution** - 需要有冲突的路由记录

这些数据会在**实际使用**双向路由时自动生成。

## 快速填充测试数据的方法

### 方法 1: 等待部署后通过 API（推荐）

1. **等待2-3分钟**，让 Railway 部署完成

2. **调用 seed 端点**创建测试数据:
```bash
curl -X POST "https://web-production-fedb.up.railway.app/admin/seed/routing-logs" \
  -H "Authorization: Bearer YOUR_NEW_TOKEN"
```

3. **刷新页面**（Cmd+Shift+R），应该能看到：
   - 5条路由日志
   - 1条冲突记录
   - PSP分布图表

### 方法 2: 通过 Employee Portal UI（更推荐）

1. **访问 Agent 详情页**：
   - https://employee.pivota.cc/dashboard/agents
   - 点击 "View" 打开 agent_ee38f2b3645a2ec2

2. **展开 "Routing Policy Configuration" 部分**

3. **编辑策略并保存**（这会触发路由逻辑）

4. **返回 Routing Management 页面**查看数据

### 方法 3: 通过实际支付（生产数据）

当 Agent 通过系统进行支付时，系统会自动：
- 评估商户和代理的路由策略
- 记录决策过程到 `routing_logs` 表
- 检测并记录任何冲突
- 显示在 Routing Management 页面

## 验证步骤

### Step 1: 刷新页面确认错误消失
```
https://employee.pivota.cc/dashboard/routing
```
- 应该看到 "No routing logs found" 而不是红色错误

### Step 2: 查看策略配置
在 Agent 详情页应该能看到：
- ✅ "Routing Policy Configuration" 部分
- ✅ 可以编辑 PSP 偏好、权重、排除列表

### Step 3: 获取新的 Employee Token

如果需要通过 API 测试，先获取新 token：
```bash
curl -X POST "https://web-production-fedb.up.railway.app/employee/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "employee@pivota.com",
    "password": "admin123"
  }'
```

## 已经创建的策略

✅ **merchant_high_risk_001**: 只允许 Stripe  
✅ **merchant_cost_sensitive_002**: 排除 Adyen，偏好 PayPal  
✅ **agent_ee38f2b3645a2ec2**: 偏好 Stripe > Adyen > PayPal（权重）

这些策略已经存在，**只是缺少实际的路由执行记录**。

## 下一步

1. **刷新页面** - 确认错误已消失
2. **等待部署完成** - 约5分钟（Railway + Vercel）
3. **调用 seed 端点** - 创建测试数据
4. **或直接使用UI** - 通过 Agent 详情页编辑策略

部署完成后，Routing Management 页面将完全功能化！🚀
