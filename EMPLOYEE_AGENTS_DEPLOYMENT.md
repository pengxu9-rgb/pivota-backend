# Employee Agents Module - 部署检查清单

## 📋 预部署检查

### 后端准备
- [ ] 复制 `employee_agents_management.py` 到后端项目
- [ ] 在 `main.py` 注册路由
- [ ] 运行数据库 migration 007
- [ ] 验证 employee 认证可用

### 前端准备
- [ ] 确认所有组件文件已创建
- [ ] 确认 `api-client.ts` 已更新
- [ ] 确认 `page.tsx` 已重写
- [ ] 运行 `npm install` 确保依赖完整

## 🚀 部署步骤

### 步骤 1: 后端部署

```bash
# 1. 进入后端项目目录
cd pivota_infra/

# 2. 复制路由文件
cp /tmp/pivota-backend-temp4/pivota_infra/routes/employee_agents_management.py \
   routes/employee_agents_management.py

# 3. 编辑 main.py，添加路由注册
# 在 imports 区域添加：
# from routes.employee_agents_management import router as employee_agents_router

# 在 app 初始化后添加：
# app.include_router(employee_agents_router)

# 4. 运行 database migration
psql $DATABASE_URL < database/migrations/007_agent_metrics.sql

# 5. 重启服务（Railway 会自动重启）
git add .
git commit -m "feat: add employee agents management API"
git push origin main
```

### 步骤 2: 前端部署

```bash
# 1. 进入前端项目目录
cd pivota-employee-portal/

# 2. 确认文件已存在
ls -la app/components/agents/
# 应该看到:
# - AgentTable.tsx
# - AgentDetailPanel.tsx
# - AgentCallsTable.tsx

# 3. 确认主页面已更新
cat app/dashboard/agents/page.tsx | head -20

# 4. 构建测试
npm run build

# 5. 本地测试（可选）
npm run dev
# 访问 http://localhost:3000/dashboard/agents

# 6. 部署到 Vercel
git add .
git commit -m "feat: complete employee agents management module"
git push origin main

# Vercel 会自动部署
```

## ✅ 部署后验证

### 1. 后端 API 测试

```bash
# 获取 employee token
export TOKEN="your_employee_token_here"

# 测试主端点
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/employee/agents

# 应该返回 JSON，包含 agents 数组

# 运行完整测试脚本
./test_employee_agents_module.sh $TOKEN
```

### 2. 前端功能测试

访问: https://pivota-employee-portal.vercel.app

#### 测试清单:
- [ ] 登录 employee 账号
- [ ] 导航到 "Agents" 页面
- [ ] 验证顶部统计卡片显示正确
- [ ] 验证 agents 列表加载
- [ ] 测试搜索功能
- [ ] 测试状态过滤
- [ ] 点击一个 agent 打开详情面板
- [ ] 验证基本信息显示
- [ ] 验证 metrics 显示
- [ ] 测试显示/隐藏 API Key
- [ ] 测试复制 API Key
- [ ] 测试编辑 Rate Limit
- [ ] 验证调用日志表格显示
- [ ] 测试调用日志分页
- [ ] 测试调用日志过滤
- [ ] 测试导出 CSV（可选）
- [ ] 测试重置 API Key（谨慎）
- [ ] 测试停用/激活（谨慎）

### 3. 数据库验证

```sql
-- 连接到生产数据库
psql $DATABASE_URL

-- 检查 agent_metrics_24h
SELECT COUNT(*) FROM agent_metrics_24h;
SELECT * FROM agent_metrics_24h LIMIT 5;

-- 检查 agent_policies
SELECT COUNT(*) FROM agent_policies;
SELECT * FROM agent_policies LIMIT 5;

-- 检查 agent_usage_logs
SELECT COUNT(*) FROM agent_usage_logs;
SELECT * FROM agent_usage_logs 
ORDER BY timestamp DESC 
LIMIT 10;
```

## 🐛 故障排除

### 问题 1: 后端 API 返回 404
**原因**: 路由未正确注册
**解决**:
```python
# 检查 main.py 中是否有:
from routes.employee_agents_management import router as employee_agents_router
app.include_router(employee_agents_router)
```

### 问题 2: 数据库查询失败
**原因**: Migration 未运行或表不存在
**解决**:
```bash
# 重新运行 migration
psql $DATABASE_URL < database/migrations/007_agent_metrics.sql

# 如果视图不存在，API 会 fallback 到实时查询
```

### 问题 3: 前端组件导入错误
**原因**: 文件路径不匹配
**解决**:
```bash
# 确认组件文件在正确位置
ls -la pivota-employee-portal/app/components/agents/

# 应该有 3 个文件:
# AgentTable.tsx
# AgentDetailPanel.tsx
# AgentCallsTable.tsx
```

### 问题 4: API 调用 401 Unauthorized
**原因**: Token 无效或过期
**解决**:
```bash
# 重新登录获取新 token
curl -X POST https://web-production-fedb.up.railway.app/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"employee@pivota.app","password":"your_password"}'
```

### 问题 5: Metrics 显示为 0
**原因**: 
- 可能是新 agent，还没有数据
- agent_metrics_24h 视图未创建或未填充数据

**解决**:
```sql
-- 手动刷新 metrics (如果使用物化视图)
REFRESH MATERIALIZED VIEW agent_metrics_24h;

-- 或者等待 API fallback 到实时计算
```

## 📊 监控和维护

### 定期检查
```bash
# 每周检查一次 agents 统计
curl -H "Authorization: Bearer $TOKEN" \
  https://web-production-fedb.up.railway.app/employee/agents \
  | jq '.total'

# 检查错误日志
# 在 Railway dashboard 查看应用日志
```

### 性能监控
```sql
-- 监控 agent_usage_logs 表大小
SELECT pg_size_pretty(pg_total_relation_size('agent_usage_logs'));

-- 如果表太大，考虑归档旧数据（> 90天）
DELETE FROM agent_usage_logs 
WHERE timestamp < NOW() - INTERVAL '90 days';
```

### 数据备份
```bash
# 定期备份 agents 相关表
pg_dump $DATABASE_URL \
  -t agents \
  -t agent_metrics_24h \
  -t agent_policies \
  -t agent_usage_logs \
  > agents_backup_$(date +%Y%m%d).sql
```

## 📈 后续优化

### 短期 (1-2周)
- [ ] 添加搜索防抖
- [ ] 添加批量操作
- [ ] 优化分页性能

### 中期 (1-2月)
- [ ] 添加图表可视化
- [ ] 实现告警规则
- [ ] WebSocket 实时更新

### 长期 (3-6月)
- [ ] AI 驱动的建议
- [ ] 高级分析报告
- [ ] 自动化运维

## ✨ 完成标志

当以下所有项都通过时，部署完成：

- ✅ 所有后端端点返回 200
- ✅ 前端页面正常加载
- ✅ 所有 UI 组件正常渲染
- ✅ 搜索和过滤功能正常
- ✅ 详情面板正常打开/关闭
- ✅ API Key 管理功能正常
- ✅ Rate Limit 更新功能正常
- ✅ 调用日志正确显示
- ✅ 分页功能正常
- ✅ CSV 导出功能正常
- ✅ 无 console 错误
- ✅ 无 linter 警告

---

**部署负责人**: _____________  
**部署日期**: _____________  
**验证人**: _____________  
**验证日期**: _____________  

