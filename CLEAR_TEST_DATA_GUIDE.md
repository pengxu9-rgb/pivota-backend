# 清理测试数据指南

## 🎯 目标

清理 Phase 4++ 测试过程中创建的所有测试路由策略和日志。

## 📊 当前测试数据

### 已创建的测试策略 (3个)
1. `merchant_high_risk_001` - 高风险商户（只允许 Stripe）
2. `merchant_cost_sensitive_002` - 成本敏感商户（排除 Adyen）
3. `agent_ee38f2b3645a2ec2` - 真实 Agent 的性能策略

### 可能存在的测试日志
- 测试订单: `test_order_*`
- 测试商户: `merchant_test_*`
- 测试代理: `agent_cost_test_*`

## 🧹 清理方法

### 方法 1: 通过 API（推荐）

**等待约5分钟**，让 Railway 部署完成后执行：

```bash
# 清理测试数据（保留真实 agent 策略）
curl -X POST "https://web-production-fedb.up.railway.app/admin/cleanup/routing-test-data" \
  -H "Authorization: Bearer <YOUR_TOKEN>"
```

**将删除**:
- merchant_high_risk_* 的所有策略和日志
- merchant_cost_sensitive_* 的所有策略和日志
- merchant_test_* 的所有策略和日志
- agent_cost_test_* 的所有策略
- 所有 test_order_* 的路由日志
- 所有 ap2_test_* 的事务

**将保留**:
- agent_ee38f2b3645a2ec2 的路由策略
- 其他真实的路由数据

### 方法 2: 手动清理（如果需要更精细控制）

```bash
# 删除特定商户策略
curl -X DELETE "https://web-production-fedb.up.railway.app/employee/routing/policies/merchant/merchant_high_risk_001" \
  -H "Authorization: Bearer <YOUR_TOKEN>"

curl -X DELETE "https://web-production-fedb.up.railway.app/employee/routing/policies/merchant/merchant_cost_sensitive_002" \
  -H "Authorization: Bearer <YOUR_TOKEN>"
```

### 方法 3: 清理所有数据（⚠️ 危险）

如果要完全重置（包括真实数据）：

```bash
curl -X POST "https://web-production-fedb.up.railway.app/admin/cleanup/all-routing-data" \
  -H "Authorization: Bearer <YOUR_TOKEN>"
```

**警告**: 这会删除所有路由策略和日志，包括生产数据！

## 📝 使用脚本清理

我已经创建了一个便捷脚本：

```bash
./cleanup_routing_test_data.sh
```

这个脚本会：
1. 显示将要删除的数据
2. 要求您确认
3. 调用清理 API
4. 显示清理结果

## ⏰ 时间线

- **现在**: Railway 正在部署 cleanup 端点（提交 303baa65）
- **+2分钟**: 后端部署完成
- **+3分钟**: 可以调用 cleanup 端点
- **+4分钟**: 刷新 Routing 页面，看到 clean slate

## 🔄 清理后的页面状态

访问 https://employee.pivota.cc/dashboard/routing 应该显示：

- Total Routings: 0 或 保留的数量
- Conflicts: 0
- Routing Trace: "No routing logs found"
- PSP Analytics: "No PSP selection data available"
- Conflict Resolution: "No conflicts detected"

## 验证清理结果

```bash
# 检查路由策略数量
curl -X GET "https://web-production-fedb.up.railway.app/employee/routing/analytics/conflict-summary?days=30" \
  -H "Authorization: Bearer <YOUR_TOKEN>" | python3 -m json.tool
```

应该看到:
```json
{
  "total_routings": 0,
  "total_conflicts": 0,
  "conflict_rate_percent": 0
}
```

## 🚀 清理后的下一步

清理完成后，您可以：

1. **开始新的测试**: 运行 `./setup_real_routing_policies.sh`
2. **配置生产策略**: 通过 UI 或 API 创建真实策略
3. **监控真实路由**: 等待实际支付时生成路由日志

---

**注意**: 等待约5分钟让部署完成，然后就可以使用清理功能了！
