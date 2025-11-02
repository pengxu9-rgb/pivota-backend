# Agents Management Phase 3 - 实施状态

## ✅ 已完成（Phase 3.1 & 3.2）

### 数据层（Database）
- ✅ Migration 009: `agent_observability.sql`
  - agent_metrics 表（时序性能数据）
  - agent_alerts 表（治理告警）
  - governance_actions_log 表（审计追踪）
- ✅ 所有索引和约束
- ✅ Migration 执行 API 端点

### 后端服务（Backend Services）
- ✅ `agent_metrics_collector.py`
  - collect_metrics_for_agent() - 从 usage_logs 聚合
  - store_metrics() - 存储到 agent_metrics
  - collect_all_agents_metrics() - 批量收集
  - get_agent_metrics_history() - 查询历史
  - cleanup_old_metrics() - 清理旧数据

- ✅ `agent_anomaly_detector.py`
  - detect_high_error_rate() - 错误率检测
  - detect_high_latency() - 延迟检测
  - detect_unusual_volume() - 流量激增检测
  - detect_rate_limit_exceeded() - 超限检测
  - create_alert() - 创建告警（去重）
  - run_anomaly_detection() - 运行所有检测

- ✅ `agent_governance_service.py`
  - propose_action() - 提议治理动作
  - execute_governance_action() - 执行（需审批）
  - reject_governance_action() - 拒绝
  - get_pending_actions() - 待审批列表
  - get_governance_history() - 历史记录

### API 端点（Backend API）
- ✅ GET /agents/{id}/metrics-history?hours=24
- ✅ GET /agents/{id}/alerts?resolved=false
- ✅ GET /agents/alerts?severity=critical&resolved=false
- ✅ POST /agents/alerts/{alert_id}/resolve
- ✅ GET /agents/{id}/health-score
- ✅ GET /admin/governance/pending-actions
- ✅ POST /admin/governance/actions/{id}/approve
- ✅ POST /admin/governance/actions/{id}/reject
- ✅ GET /admin/governance/agents/{id}/governance-history
- ✅ POST /admin/governance/metrics/collect-now

---

## ⏳ Phase 3.3 待完成（Frontend UI）

### 需要创建的组件

1. **AgentMetricsChart.tsx**
   - 使用 Recharts
   - 显示 response time, error rate, qpm 曲线
   - 30秒轮询刷新
   - 时间范围选择（1h, 6h, 24h, 7d）

2. **AgentAlertsPanel.tsx**
   - 告警列表（表格或卡片）
   - 严重程度 badge（info/warning/critical）
   - Resolve 按钮
   - 按严重程度过滤

3. **GovernanceActionsPanel.tsx**
   - 待审批动作表格
   - Approve/Reject 按钮
   - 显示 agent、动作类型、原因
   - 历史记录查看

4. **更新 AgentDetailPanel**
   - 添加 Metrics History 折叠区域
   - 添加 Alerts 折叠区域（显示未解决的）
   - 添加 Health Score badge

5. **更新 agents/page.tsx**
   - 顶部告警横幅（如果有critical alerts）
   - Health score 列（可选）

---

## 🚀 当前可用功能

### 数据收集
```bash
# 手动触发收集（部署后测试）
curl -X POST "$API_URL/admin/governance/metrics/collect-now" \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

### 查询 Metrics
```bash
# 查看 agent 性能历史
curl "$API_URL/employee/agents/agent_xxx/metrics-history?hours=24" \
  -H "Authorization: Bearer TOKEN"
```

### 查询 Alerts
```bash
# 查看所有未解决的 critical alerts
curl "$API_URL/employee/agents/alerts?severity=critical&resolved=false" \
  -H "Authorization: Bearer TOKEN"
```

### Health Score
```bash
# 计算 agent 健康分数
curl "$API_URL/employee/agents/agent_xxx/health-score" \
  -H "Authorization: Bearer TOKEN"
```

### 治理审批
```bash
# 查看待审批动作
curl "$API_URL/admin/governance/pending-actions" \
  -H "Authorization: Bearer ADMIN_TOKEN"

# 批准动作
curl -X POST "$API_URL/admin/governance/actions/action_xxx/approve" \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

---

## 📋 部署步骤

### 1. Railway Redeploy（Backend）
- 最新 commit: 8c1754db
- 包含所有 Phase 3.1 & 3.2 功能

### 2. 运行 Migration 009
```bash
./run_migration_009.sh YOUR_ADMIN_TOKEN
```

### 3. 测试 Metrics 收集
```bash
# 手动触发一次收集
curl -X POST "https://web-production-fedb.up.railway.app/admin/governance/metrics/collect-now" \
  -H "Authorization: Bearer YOUR_TOKEN"

# 查看收集的数据
curl "https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/metrics-history?hours=1" \
  -H "Authorization: Bearer YOUR_TOKEN" | python3 -m json.tool
```

### 4. 测试 Health Score
```bash
curl "https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/health-score" \
  -H "Authorization: Bearer YOUR_TOKEN" | python3 -m json.tool
```

---

## 🎯 Phase 3 功能概览

### Metrics Collection（已实现）
- ✅ 每 5 分钟收集一次（手动触发）
- ✅ 从 agent_usage_logs 聚合
- ✅ 存储到 agent_metrics 表
- ✅ 可查询历史数据

### Anomaly Detection（已实现）
- ✅ 高错误率检测（> policy threshold）
- ✅ 高延迟检测（> 5000ms）
- ✅ 流量激增检测（3x baseline）
- ✅ 超限检测（> rate_limit）
- ✅ 自动创建告警（去重）

### Semi-Auto Governance（已实现）
- ✅ 动作提议（reduce_rate_limit, suspend, key_rotation, warning）
- ✅ 人工审批流程（approve/reject）
- ✅ 审计日志
- ✅ 动作执行

### Health Score（已实现）
- ✅ 0-100 分数
- ✅ A-F 等级
- ✅ 基于多因素（success rate, latency, staleness, alerts）
- ✅ 详细的扣分说明

---

## ⏳ 待完成（Phase 3.3 - UI）

### Frontend 组件
- [ ] AgentMetricsChart.tsx（30s 轮询刷新）
- [ ] AgentAlertsPanel.tsx（告警列表和解决）
- [ ] GovernanceActionsPanel.tsx（审批界面）
- [ ] AgentDetailPanel 集成（Metrics/Alerts tabs）
- [ ] agents/page.tsx 集成（Critical alerts banner）

### API Client
- [ ] getAgentMetricsHistory()
- [ ] getAgentAlerts()
- [ ] resolveAlert()
- [ ] getPendingGovernanceActions()
- [ ] approveGovernanceAction()
- [ ] rejectGovernanceAction()

### 数据轮询
- [ ] 30秒定时器刷新 metrics
- [ ] 告警数量实时更新
- [ ] 待审批动作数量提示

---

## 🔄 自动化 TODO（未来）

### Metrics 收集自动化
当前：手动触发 POST /metrics/collect-now

未来选项：
1. **FastAPI BackgroundTasks**
   - 在 startup 时启动后台任务
   - 每 5 分钟循环执行
   
2. **Cron Job**
   - 系统 crontab 调用 API
   - `*/5 * * * * curl -X POST .../collect-now`

3. **APScheduler**
   - pip install apscheduler
   - 在 main.py 中配置定时任务

---

## 📊 数据流程

```
agent_usage_logs (raw data)
    ↓ (每 5 分钟聚合)
agent_metrics (时序数据)
    ↓ (检测异常)
agent_alerts (告警)
    ↓ (生成提议)
governance_actions_log (pending)
    ↓ (人工审批)
governance_actions_log (approved)
    ↓ (执行策略)
agents 表更新 (rate_limit, status)
```

---

## ✅ 验证清单

### 后端（部署后）
- [ ] Railway 部署成功
- [ ] Migration 009 运行成功
- [ ] agent_metrics 表存在
- [ ] agent_alerts 表存在
- [ ] governance_actions_log 表存在
- [ ] Metrics collection 手动触发成功
- [ ] Health score API 返回正确分数

### 功能测试
- [ ] 收集 metrics 后能查到历史数据
- [ ] 异常检测能创建告警
- [ ] 告警能被解决
- [ ] 治理动作能被批准/拒绝
- [ ] 审计日志记录完整

---

## 📝 下一步

1. **立即**：部署并运行 Migration 009
2. **然后**：手动触发 metrics 收集测试
3. **接下来**：实施 Phase 3.3 Frontend UI
4. **最后**：添加自动调度器

---

**Backend Phase 3.1 & 3.2 完成！等待部署后可测试 metrics 和 alerts 功能。** 🚀
