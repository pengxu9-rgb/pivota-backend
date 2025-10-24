# 端点清理计划

## 现状分析
- 总端点数：656个
- 路由文件：78个
- 冲突端点：300个（重复定义）
- 临时/调试端点：136个

## 修复原则
1. **保持稳定性** - 不破坏现有功能
2. **渐进式修复** - 分阶段进行
3. **向后兼容** - 保留必要的旧端点
4. **充分测试** - 每步都要验证

## 第一阶段：修复重复路由包含（低风险）

### 问题
在 main.py 中，以下路由被重复包含：
- `agent_metrics_router` - 行 36, 195, 216（包含3次！）
- `dashboard_router` 和相关路由

### 修复步骤
1. 删除重复的 include_router 调用
2. 保留最合适的一个
3. 确保所有端点仍然可访问

## 第二阶段：整理端点冲突（中风险）

### 主要冲突类型
1. **完全重复** - 同一文件内的重复定义（可能是复制粘贴错误）
   - 例如：agents_mgmt.py 中的多个重复端点
   
2. **功能重叠** - 不同文件实现相同功能
   - agent_sdk_ready.py vs agent_sdk_fixed.py
   - agent_metrics.py vs agent_metrics_v1.py

### 修复策略
- 保留更完整/更新的实现
- 为旧端点创建别名/重定向
- 添加弃用警告

## 第三阶段：移除临时端点（高风险）

### 需要保留但加强安全的端点
- `/setup/create-all-indexes` - 部署时需要
- `/admin/simulate/payments` - 测试需要

### 可以安全移除的端点
- 各种 test/debug 端点
- 重复的 fix/init 端点

## 第四阶段：重组路由文件

### 合并相似功能
1. **Agent 相关**
   - agent_api.py (主要)
   - agent_sdk_ready.py → 合并到 agent_api.py
   - agent_sdk_fixed.py → 合并到 agent_api.py
   
2. **Merchant 相关**
   - 保持现有结构（已经组织良好）

## 实施计划

### 今天（第一阶段）
1. 修复 main.py 重复包含
2. 创建端点映射文档
3. 测试所有关键端点

### 明天（第二阶段）
1. 解决文件内部重复
2. 合并功能重叠的路由
3. 添加重定向和弃用警告

### 后续（第三、四阶段）
1. 逐步移除临时端点
2. 重组路由文件结构
3. 更新文档

## 测试清单

### 关键端点测试
- [ ] Agent API: `/agent/v1/health`
- [ ] Agent API: `/agent/v1/orders/create`
- [ ] Merchant: `/merchant/onboarding/all`
- [ ] Auth: `/auth/signin`
- [ ] Admin: `/admin/analytics/overview`

### 集成测试
- [ ] Agent Portal 登录和功能
- [ ] Employee Portal 所有页面
- [ ] SDK 调用测试
