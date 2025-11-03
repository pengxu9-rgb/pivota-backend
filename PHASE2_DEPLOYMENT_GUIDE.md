# Agents Management Phase 2 - 部署指南

## 已完成的功能

### Backend（Commit: f164fe1c）
- ✅ 数据库迁移 008_agents_advanced_schema.sql
  - agent_api_keys 表（多 API Key 支持）
  - agent_protocols 表（协议追踪）
  - agent_performance_stats 表（性能统计）
- ✅ 10 个新 API 端点（Keys, Protocols, Performance）
- ✅ 详情端点扩展（返回 api_keys 和 protocols 数组）

### Frontend（Commit: e6f5a68）
- ✅ 类型定义扩展（AgentApiKey, AgentProtocol）
- ✅ API 客户端方法（9 个新方法）
- ✅ AgentDetailPanel UI 扩展
  - API Keys 列表显示
  - Protocols badges 显示

---

## 部署步骤

### 1. 数据库迁移

**Railway Dashboard → Database → Query**：

运行迁移脚本：
```bash
cat pivota_infra/db/migrations/008_agents_advanced_schema.sql
```

或通过 SQL 编辑器粘贴并执行。

**验证表创建**：
```sql
SELECT table_name FROM information_schema.tables 
WHERE table_name IN ('agent_api_keys', 'agent_protocols', 'agent_performance_stats');
```

应该返回 3 行。

### 2. 后端部署

**Railway 会自动部署**（检测到 GitHub push）

**或手动触发**：
- Railway Dashboard → Backend Service → Deploy

**等待时间**：约 2-3 分钟

**验证部署**：
```bash
curl -I https://web-production-fedb.up.railway.app/docs
```
应该返回 200

### 3. 前端部署

**Vercel 会自动部署**

**验证时间**：约 1-2 分钟

---

## 功能测试

### 使用测试脚本（推荐）

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

./test_phase2_agents.sh YOUR_ADMIN_TOKEN
```

**测试内容**：
1. 获取详情（验证 api_keys 和 protocols 数组）
2. 列出 API Keys
3. 创建新 API Key
4. 列出 Protocols
5. 添加 GraphQL 协议
6. 获取性能统计
7. 撤销测试 Key（清理）

### 手动测试

#### 测试 1: 查看详情（包含新字段）
```bash
curl -sS 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2' \
  -H 'Authorization: Bearer TOKEN' | python3 -m json.tool
```

**期望看到**：
```json
{
  "agent": {
    "api_keys": [...],
    "protocols": [...]
  }
}
```

#### 测试 2: 创建新 API Key
```bash
curl -sS -X POST 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/api-keys' \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "scopes": ["orders:read", "products:read"],
    "ip_whitelist": [],
    "expires_in_days": 90
  }' | python3 -m json.tool
```

**期望返回**：
```json
{
  "status": "success",
  "api_key": "ak_live_...",  // 完整 key，只显示一次
  "key_id": "key_...",
  "key_prefix": "ak_live_01..."
}
```

#### 测试 3: 添加协议
```bash
curl -sS -X POST 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/protocols' \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "protocol_name": "REST",
    "version": "1.0"
  }' | python3 -m json.tool
```

---

## UI 验证

### 1. 刷新 Employee Portal

访问：https://employee.pivota.cc/dashboard/agents

### 2. 点击 Agent 的 "View" 按钮

### 3. 详情弹窗中应该看到（如果有数据）：

**API Keys Section**（新增）：
- 显示多个 API Keys（如果有）
- 每个 key 显示：prefix, scopes, status, expiry
- 提示："Generate, revoke via API (UI coming Phase 3)"

**Protocols Section**（新增）：
- Badge 形式显示：REST v1.0 (active), GraphQL v2023 (active)
- 提示："Protocol management UI coming Phase 3"

**原有功能**：
- ✅ 基本信息
- ✅ API Key 管理（单个 key，Phase 1）
- ✅ Performance Metrics
- ✅ Governance Policies

---

## 数据迁移效果

### 自动迁移逻辑

迁移脚本会：
1. 为现有 agents 的 api_key 创建对应的 agent_api_keys 记录
2. 为所有现有 agents 添加默认 REST 协议支持
3. agent_performance_stats 表为空（需要后续聚合任务填充）

### 验证迁移

```sql
-- 检查现有 agent 是否有对应的 api_keys 记录
SELECT 
    a.agent_id,
    a.name,
    COUNT(ak.id) as api_keys_count,
    COUNT(ap.id) as protocols_count
FROM agents a
LEFT JOIN agent_api_keys ak ON a.agent_id = ak.agent_id
LEFT JOIN agent_protocols ap ON a.agent_id = ap.agent_id
GROUP BY a.agent_id, a.name;
```

**期望**：每个 agent 至少 1 个 api_key 和 1 个 protocol（REST）

---

## 故障排除

### 问题 1: 详情端点返回 500 错误

**原因**：新表未创建或迁移失败

**解决**：
1. 手动运行迁移 SQL
2. 检查 Railway logs 中的错误

### 问题 2: API Keys/Protocols 不显示

**原因**：迁移数据未填充

**解决**：
```sql
-- 手动为测试 agent 添加数据
INSERT INTO agent_api_keys (agent_id, key_id, key_hash, key_prefix, scopes, is_active)
VALUES ('agent_ee38f2b3645a2ec2', 'key_test1', 'hash123', 'ak_live_01...', '["orders:read"]', true);

INSERT INTO agent_protocols (agent_id, protocol_name, version, status)
VALUES ('agent_ee38f2b3645a2ec2', 'REST', '1.0', 'active');
```

### 问题 3: Frontend 不显示新部分

**原因**：
- Vercel 部署未完成
- 浏览器缓存

**解决**：
1. 等待 Vercel 部署完成
2. 强制刷新（Cmd+Shift+R）
3. 检查 agent.api_keys 和 agent.protocols 是否有数据

---

## API 端点总览

### 新增端点（Phase 2）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/employee/agents/{id}/api-keys` | GET | 列出所有 Keys |
| `/employee/agents/{id}/api-keys` | POST | 创建新 Key |
| `/employee/agents/{id}/api-keys/{key_id}` | DELETE | 撤销 Key |
| `/employee/agents/{id}/api-keys/{key_id}/rotate` | POST | 轮换 Key |
| `/employee/agents/{id}/protocols` | GET | 列出协议 |
| `/employee/agents/{id}/protocols` | POST | 添加协议 |
| `/employee/agents/{id}/protocols/{id}` | PUT | 更新协议状态 |
| `/employee/agents/{id}/performance` | GET | 性能统计 |

### 更新的端点

| 端点 | 变化 |
|------|------|
| `GET /employee/agents/{id}` | 新增 api_keys, protocols 数组 |

---

## Next Steps (Phase 3)

完成 Phase 2 后，未来可添加：

1. **UI 管理按钮**
   - Generate Key 按钮 + Modal
   - Revoke/Rotate 按钮
   - Add Protocol 按钮 + Modal

2. **实时性能图表**
   - WebSocket 连接
   - Recharts/Chart.js 可视化
   - 实时请求监控

3. **高级治理**
   - 异常检测告警
   - 自动策略执行
   - OpenTelemetry 集成

---

## ✅ 部署清单

- [ ] Railway 运行数据库迁移 008
- [ ] Railway Redeploy 后端（自动或手动）
- [ ] Vercel 部署前端（自动）
- [ ] 运行测试脚本验证功能
- [ ] 刷新 Employee Portal 查看新 UI

---

**所有代码已推送！等待部署完成后运行测试脚本验证。** 🚀


## 已完成的功能

### Backend（Commit: f164fe1c）
- ✅ 数据库迁移 008_agents_advanced_schema.sql
  - agent_api_keys 表（多 API Key 支持）
  - agent_protocols 表（协议追踪）
  - agent_performance_stats 表（性能统计）
- ✅ 10 个新 API 端点（Keys, Protocols, Performance）
- ✅ 详情端点扩展（返回 api_keys 和 protocols 数组）

### Frontend（Commit: e6f5a68）
- ✅ 类型定义扩展（AgentApiKey, AgentProtocol）
- ✅ API 客户端方法（9 个新方法）
- ✅ AgentDetailPanel UI 扩展
  - API Keys 列表显示
  - Protocols badges 显示

---

## 部署步骤

### 1. 数据库迁移

**Railway Dashboard → Database → Query**：

运行迁移脚本：
```bash
cat pivota_infra/db/migrations/008_agents_advanced_schema.sql
```

或通过 SQL 编辑器粘贴并执行。

**验证表创建**：
```sql
SELECT table_name FROM information_schema.tables 
WHERE table_name IN ('agent_api_keys', 'agent_protocols', 'agent_performance_stats');
```

应该返回 3 行。

### 2. 后端部署

**Railway 会自动部署**（检测到 GitHub push）

**或手动触发**：
- Railway Dashboard → Backend Service → Deploy

**等待时间**：约 2-3 分钟

**验证部署**：
```bash
curl -I https://web-production-fedb.up.railway.app/docs
```
应该返回 200

### 3. 前端部署

**Vercel 会自动部署**

**验证时间**：约 1-2 分钟

---

## 功能测试

### 使用测试脚本（推荐）

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

./test_phase2_agents.sh YOUR_ADMIN_TOKEN
```

**测试内容**：
1. 获取详情（验证 api_keys 和 protocols 数组）
2. 列出 API Keys
3. 创建新 API Key
4. 列出 Protocols
5. 添加 GraphQL 协议
6. 获取性能统计
7. 撤销测试 Key（清理）

### 手动测试

#### 测试 1: 查看详情（包含新字段）
```bash
curl -sS 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2' \
  -H 'Authorization: Bearer TOKEN' | python3 -m json.tool
```

**期望看到**：
```json
{
  "agent": {
    "api_keys": [...],
    "protocols": [...]
  }
}
```

#### 测试 2: 创建新 API Key
```bash
curl -sS -X POST 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/api-keys' \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "scopes": ["orders:read", "products:read"],
    "ip_whitelist": [],
    "expires_in_days": 90
  }' | python3 -m json.tool
```

**期望返回**：
```json
{
  "status": "success",
  "api_key": "ak_live_...",  // 完整 key，只显示一次
  "key_id": "key_...",
  "key_prefix": "ak_live_01..."
}
```

#### 测试 3: 添加协议
```bash
curl -sS -X POST 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2/protocols' \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "protocol_name": "REST",
    "version": "1.0"
  }' | python3 -m json.tool
```

---

## UI 验证

### 1. 刷新 Employee Portal

访问：https://employee.pivota.cc/dashboard/agents

### 2. 点击 Agent 的 "View" 按钮

### 3. 详情弹窗中应该看到（如果有数据）：

**API Keys Section**（新增）：
- 显示多个 API Keys（如果有）
- 每个 key 显示：prefix, scopes, status, expiry
- 提示："Generate, revoke via API (UI coming Phase 3)"

**Protocols Section**（新增）：
- Badge 形式显示：REST v1.0 (active), GraphQL v2023 (active)
- 提示："Protocol management UI coming Phase 3"

**原有功能**：
- ✅ 基本信息
- ✅ API Key 管理（单个 key，Phase 1）
- ✅ Performance Metrics
- ✅ Governance Policies

---

## 数据迁移效果

### 自动迁移逻辑

迁移脚本会：
1. 为现有 agents 的 api_key 创建对应的 agent_api_keys 记录
2. 为所有现有 agents 添加默认 REST 协议支持
3. agent_performance_stats 表为空（需要后续聚合任务填充）

### 验证迁移

```sql
-- 检查现有 agent 是否有对应的 api_keys 记录
SELECT 
    a.agent_id,
    a.name,
    COUNT(ak.id) as api_keys_count,
    COUNT(ap.id) as protocols_count
FROM agents a
LEFT JOIN agent_api_keys ak ON a.agent_id = ak.agent_id
LEFT JOIN agent_protocols ap ON a.agent_id = ap.agent_id
GROUP BY a.agent_id, a.name;
```

**期望**：每个 agent 至少 1 个 api_key 和 1 个 protocol（REST）

---

## 故障排除

### 问题 1: 详情端点返回 500 错误

**原因**：新表未创建或迁移失败

**解决**：
1. 手动运行迁移 SQL
2. 检查 Railway logs 中的错误

### 问题 2: API Keys/Protocols 不显示

**原因**：迁移数据未填充

**解决**：
```sql
-- 手动为测试 agent 添加数据
INSERT INTO agent_api_keys (agent_id, key_id, key_hash, key_prefix, scopes, is_active)
VALUES ('agent_ee38f2b3645a2ec2', 'key_test1', 'hash123', 'ak_live_01...', '["orders:read"]', true);

INSERT INTO agent_protocols (agent_id, protocol_name, version, status)
VALUES ('agent_ee38f2b3645a2ec2', 'REST', '1.0', 'active');
```

### 问题 3: Frontend 不显示新部分

**原因**：
- Vercel 部署未完成
- 浏览器缓存

**解决**：
1. 等待 Vercel 部署完成
2. 强制刷新（Cmd+Shift+R）
3. 检查 agent.api_keys 和 agent.protocols 是否有数据

---

## API 端点总览

### 新增端点（Phase 2）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/employee/agents/{id}/api-keys` | GET | 列出所有 Keys |
| `/employee/agents/{id}/api-keys` | POST | 创建新 Key |
| `/employee/agents/{id}/api-keys/{key_id}` | DELETE | 撤销 Key |
| `/employee/agents/{id}/api-keys/{key_id}/rotate` | POST | 轮换 Key |
| `/employee/agents/{id}/protocols` | GET | 列出协议 |
| `/employee/agents/{id}/protocols` | POST | 添加协议 |
| `/employee/agents/{id}/protocols/{id}` | PUT | 更新协议状态 |
| `/employee/agents/{id}/performance` | GET | 性能统计 |

### 更新的端点

| 端点 | 变化 |
|------|------|
| `GET /employee/agents/{id}` | 新增 api_keys, protocols 数组 |

---

## Next Steps (Phase 3)

完成 Phase 2 后，未来可添加：

1. **UI 管理按钮**
   - Generate Key 按钮 + Modal
   - Revoke/Rotate 按钮
   - Add Protocol 按钮 + Modal

2. **实时性能图表**
   - WebSocket 连接
   - Recharts/Chart.js 可视化
   - 实时请求监控

3. **高级治理**
   - 异常检测告警
   - 自动策略执行
   - OpenTelemetry 集成

---

## ✅ 部署清单

- [ ] Railway 运行数据库迁移 008
- [ ] Railway Redeploy 后端（自动或手动）
- [ ] Vercel 部署前端（自动）
- [ ] 运行测试脚本验证功能
- [ ] 刷新 Employee Portal 查看新 UI

---

**所有代码已推送！等待部署完成后运行测试脚本验证。** 🚀

