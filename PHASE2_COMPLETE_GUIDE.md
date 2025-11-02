# Agents Management Phase 2 - 完整部署指南

## ✅ 已完成的实施

### Backend（3个提交）
1. **f164fe1c** - Phase 2 核心功能
   - 数据库迁移 SQL
   - 8 个新 API 端点
   - 详情端点扩展

2. **d09b3785** - 迁移执行 API
   - `/admin/migrations/run-008-agents-phase2` - 执行迁移
   - `/admin/migrations/check-008-status` - 检查状态

### Frontend（1个提交）
1. **e6f5a68** - UI 和类型扩展
   - AgentApiKey, AgentProtocol 接口
   - 9 个新 API 客户端方法
   - AgentDetailPanel 新部分

---

## 🚀 快速部署（3步完成）

### 步骤 1: 等待 Railway 部署（2-3分钟）

Railway 会自动检测 GitHub push 并部署。

**验证部署完成**：
```bash
curl -I https://web-production-fedb.up.railway.app/health
```
应返回 200

### 步骤 2: 执行数据库迁移（通过 API）

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

./run_migration_008.sh YOUR_ADMIN_TOKEN
```

**脚本会自动**：
1. 检查迁移状态
2. 询问确认
3. 执行迁移（创建3个表）
4. 迁移现有数据
5. 验证结果

**预期输出**：
```json
{
  "status": "success",
  "message": "Migration 008 completed successfully",
  "steps": [
    "✅ agent_api_keys table created",
    "✅ agent_protocols table created",
    "✅ agent_performance_stats table created",
    "✅ Migrated existing API keys: 1 records",
    "✅ Added default REST protocol: 1 records"
  ],
  "verification": {
    "agent_api_keys_count": 1,
    "agent_protocols_count": 1,
    "agent_performance_stats_count": 0
  }
}
```

### 步骤 3: 刷新前端验证（Cmd+Shift+R）

访问：https://employee.pivota.cc/dashboard/agents

点击 Agent 的 "View" 按钮，应该看到：

**新增部分**：
- 📦 **API Keys (1)** - 显示迁移的现有 key
- 🔌 **Supported Protocols (1)** - 显示 REST v1.0 badge

---

## 🧪 功能测试

### 完整测试脚本

```bash
./test_phase2_agents.sh YOUR_ADMIN_TOKEN
```

**测试内容**：
1. ✅ 获取详情（验证 api_keys 和 protocols）
2. ✅ 列出 API Keys
3. ✅ 创建新 API Key（带自定义 scopes）
4. ✅ 列出 Protocols
5. ✅ 添加 GraphQL 协议
6. ✅ 查询性能统计
7. ✅ 撤销测试 Key（清理）

### 手动测试示例

#### 创建新 API Key
```bash
curl -X POST "https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/api-keys" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "scopes": ["orders:read", "products:read", "orders:write"],
    "ip_whitelist": ["192.168.1.0/24"],
    "expires_in_days": 90
  }'
```

**响应**：
```json
{
  "status": "success",
  "api_key": "ak_live_abc123...",  // 完整 key，只显示一次！
  "key_id": "key_xyz789",
  "key_prefix": "ak_live_abc...",
  "scopes": ["orders:read", "products:read", "orders:write"]
}
```

#### 添加协议
```bash
curl -X POST "https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/protocols" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol_name": "GraphQL",
    "version": "2023"
  }'
```

---

## 📋 新功能概览

### 1. 多 API Key 管理

**能力**：
- ✅ 每个 agent 可以有多个 API keys
- ✅ 每个 key 独立的 scopes（权限范围）
- ✅ IP 白名单限制
- ✅ 过期时间设置
- ✅ Key 轮换（rotate）
- ✅ 撤销（revoke）

**使用场景**：
- 不同环境使用不同 key（dev, staging, prod）
- 不同权限的 key（只读 vs 读写）
- 定期轮换 key 提高安全性

### 2. 协议追踪

**能力**：
- ✅ 记录 agent 支持的协议
- ✅ 版本管理
- ✅ 状态标记（active/deprecated/disabled）
- ✅ 合规性追踪

**支持的协议**：
- REST（默认）
- GraphQL
- WebSocket
- gRPC（可扩展）

### 3. 性能统计

**能力**：
- ✅ 日粒度聚合数据
- ✅ Fallback 到实时计算
- ✅ 多时间范围查询（1d, 7d, 30d, 90d）

**指标**：
- total_requests, success_count, fail_count
- success_rate, avg_latency_ms
- total_gmv, total_orders

---

## 🎨 UI 预览

### AgentDetailPanel 新增部分

#### API Keys Section
```
┌─────────────────────────────────────┐
│ 🔑 API Keys (2)                     │
├─────────────────────────────────────┤
│ ┌─────────────────────────────────┐ │
│ │ ak_live_01...    [Active]       │ │
│ │ Scopes: orders:read, products:r │ │
│ │ Expires: 2026-02-01             │ │
│ │ Last Used: 2025-11-01 10:30 PM  │ │
│ └─────────────────────────────────┘ │
│ ┌─────────────────────────────────┐ │
│ │ ak_live_xyz...   [Active]       │ │
│ │ Scopes: orders:read             │ │
│ └─────────────────────────────────┘ │
│ Note: Generate, revoke via API     │
│ (UI coming in Phase 3)             │
└─────────────────────────────────────┘
```

#### Protocols Section
```
┌─────────────────────────────────────┐
│ 🔌 Supported Protocols (2)          │
├─────────────────────────────────────┤
│ [REST v1.0]  [GraphQL v2023]       │
│                                     │
│ Note: Protocol management UI       │
│ coming in Phase 3                  │
└─────────────────────────────────────┘
```

---

## ⚠️ 重要提示

### 向后兼容
- ✅ 现有 `agents.api_key` 字段保留
- ✅ 单 key 系统继续正常工作
- ✅ 旧 API 响应格式不变
- ✅ 新字段可选（api_keys, protocols）

### 数据迁移
- ✅ 自动为现有 agents 创建 api_keys 记录
- ✅ 自动添加 REST 协议
- ✅ 无数据丢失风险
- ✅ 可重复执行（ON CONFLICT DO NOTHING）

### 安全性
- ✅ 完整 API key 只在创建/轮换时显示一次
- ✅ 存储 key_hash 而非明文
- ✅ 审计日志记录所有操作
- ✅ admin 角色才能管理

---

## 📊 数据流程

### API Key 生命周期
```
创建 → 返回完整 key（保存！）
    ↓
使用 → 更新 last_used_at
    ↓
轮换 → 旧 key 失效，生成新 key
    ↓
撤销 → is_active = false
```

### Protocol 管理
```
添加 → status = active
    ↓
验证 → 更新 last_verified_at
    ↓
弃用 → status = deprecated
    ↓
禁用 → status = disabled
```

---

## 🔧 故障排除

### 问题：迁移失败

**检查**：
```bash
curl -sS 'https://web-production-fedb.up.railway.app/admin/migrations/check-008-status' \
  -H 'Authorization: Bearer YOUR_TOKEN' | python3 -m json.tool
```

**常见原因**：
1. agents 表不存在 → 先运行之前的迁移
2. FK 约束失败 → 检查 agent_id 是否有效
3. 权限问题 → 确保使用 admin token

### 问题：UI 不显示新部分

**检查**：
1. Vercel 部署是否完成
2. 浏览器缓存（强制刷新）
3. API 是否返回 api_keys 和 protocols

**调试**：
```bash
# 检查详情端点响应
curl -sS 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2' \
  -H 'Authorization: Bearer YOUR_TOKEN' | python3 -m json.tool | grep -A 10 api_keys
```

---

## ✅ 完成清单

### 部署
- [ ] Railway 自动部署完成（Backend）
- [ ] Vercel 自动部署完成（Frontend）

### 迁移
- [ ] 运行 `./run_migration_008.sh YOUR_ADMIN_TOKEN`
- [ ] 验证3个表已创建
- [ ] 验证现有 agent 有 api_keys 和 protocols 记录

### 验证
- [ ] 运行 `./test_phase2_agents.sh` 测试所有端点
- [ ] 刷新 Employee Portal 查看新 UI
- [ ] 创建测试 API key 成功
- [ ] 添加测试协议成功

### 功能确认
- [ ] 详情弹窗显示 API Keys section
- [ ] 详情弹窗显示 Protocols section
- [ ] 原有功能（列表、创建、停用等）完全正常
- [ ] 无任何报错或数据丢失

---

## 📈 下一步（Phase 3）

Phase 2 完成后，可以添加：

1. **完整的 Key 管理 UI**
   - Generate Key Modal（Scopes 复选框，IP 输入）
   - Revoke 确认对话框
   - Rotate 按钮

2. **Protocol 管理 UI**
   - Add Protocol Modal
   - Status 切换按钮
   - Version 升级功能

3. **实时性能监控**
   - WebSocket 连接
   - 实时图表（Recharts）
   - 告警功能

4. **聚合任务**
   - 定时任务填充 agent_performance_stats
   - 每日凌晨聚合前一天数据

---

## 🎊 总结

**Phase 2 为 Agents Management 添加了企业级功能**：
- ✅ 多 Key 支持（安全性↑）
- ✅ 协议追踪（合规性↑）
- ✅ 性能统计（可见性↑）

**不破坏现有功能**：
- ✅ Phase 1 完全稳定
- ✅ 向后兼容
- ✅ 可选功能

**为 Phase 3 做好准备**：
- ✅ 数据结构完整
- ✅ API 端点齐全
- ✅ 只差 UI 按钮

---

**立即行动**：
1. 等待 Railway 部署完成
2. 运行 `./run_migration_008.sh YOUR_ADMIN_TOKEN`
3. 刷新前端验证

**所有代码已推送，准备部署！** 🚀

