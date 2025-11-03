# 验证 Phase 2 前端部署

## 前端没有变化的原因

### 可能原因 1: Vercel 还没部署完成 ⏳

**检查方法**：
1. 登录 Vercel Dashboard
2. 查看 `pivota-employee-portal` 项目
3. 检查 Deployments 页面
4. 最新部署应该是 commit `e6f5a68`
5. 状态应该是 "Ready"（绿色）

### 可能原因 2: 浏览器缓存 🔄

**解决方法**：
1. 打开 https://employee.pivota.cc/dashboard/agents
2. 打开开发者工具（F12）
3. 右键点击刷新按钮
4. 选择 "清空缓存并硬性重新加载"
5. 或按 `Cmd+Shift+R`（Mac）/ `Ctrl+Shift+F5`（Windows）

### 可能原因 3: 需要手动触发部署

**如果 Vercel 没有自动部署**：
1. Vercel Dashboard → pivota-employee-portal 项目
2. Deployments → 最新的 commit
3. 点击 "Redeploy"

---

## 验证前端代码已更新

### 方法 1: 检查浏览器控制台

打开 https://employee.pivota.cc/dashboard/agents

**F12 → Console**，查找：
```
🔧 [API Client] Initializing with baseURL: https://...
```

### 方法 2: 检查页面源码

**F12 → Sources → webpack**

搜索文件：`AgentDetailPanel`

应该能找到包含 "API Keys" 和 "Supported Protocols" 的代码。

### 方法 3: 测试 API 调用

打开详情弹窗，**F12 → Network**

应该看到：
```
GET /employee/agents/agent_ee38f2b3645a2ec2
Response: {
  "agent": {
    "api_keys": [...],
    "protocols": [...]
  }
}
```

如果 response 有 api_keys 和 protocols，但 UI 不显示：
- 说明前端代码还没更新
- 需要等待 Vercel 部署或强制刷新

---

## 预期的 UI 变化

### 详情弹窗中应该新增两个部分：

#### 在 "Governance Policies" 之后

**1. API Keys Section**（蓝色背景）
```
┌─────────────────────────────────────┐
│ 🔑 API Keys (1)                     │
│ ┌─────────────────────────────────┐ │
│ │ ak_live_886c...  [Active]       │ │
│ │ Scopes: orders:read, products:  │ │
│ │         read, orders:write      │ │
│ │ Created: 10/27/2025, ...        │ │
│ └─────────────────────────────────┘ │
│ Note: Generate, revoke, and rotate │
│ keys via API endpoints             │
│ (UI coming in Phase 3)             │
└─────────────────────────────────────┘
```

**2. Protocols Section**（紫色背景）
```
┌─────────────────────────────────────┐
│ 🔌 Supported Protocols (1)          │
│ [REST v1.0] (绿色 badge)            │
│ Protocol management UI coming in   │
│ Phase 3                            │
└─────────────────────────────────────┘
```

---

## 故障排除

### 问题：强制刷新后仍无变化

**检查 Vercel 部署**：
```bash
# 检查最新部署时间
# 应该是几分钟前
```

**手动触发部署**：
1. Vercel Dashboard
2. pivota-employee-portal 项目
3. Deployments 标签
4. 点击 "Redeploy"
5. 选择最新 commit（e6f5a68）

### 问题：UI 显示但数据格式错误

**刚修复了 scopes 格式问题**（Commit: 1a625f21）

需要：
1. Railway Redeploy（等待 2-3 分钟）
2. 刷新前端查看

**正确的 scopes 显示**：
```
Scopes: orders:read, products:read, orders:write
```

**不应该是**：
```
Scopes: ["orders:read", "products:read", "orders:write"]
```

---

## 调试步骤

### 1. 确认 Vercel 部署状态

Vercel Dashboard 应该显示：
- Latest Deployment: e6f5a68
- Status: Ready
- Time: 最近几分钟

### 2. 确认后端返回正确数据

```bash
curl -sS 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2' \
  -H 'Authorization: Bearer YOUR_TOKEN' | python3 -c "
import sys, json
data = json.load(sys.stdin)
agent = data.get('agent', {})
print(f'Has api_keys: {\"api_keys\" in agent}')
print(f'Has protocols: {\"protocols\" in agent}')
if 'api_keys' in agent:
    print(f'API Keys count: {len(agent[\"api_keys\"])}')
    if agent['api_keys']:
        scopes = agent['api_keys'][0].get('scopes')
        print(f'Scopes type: {type(scopes).__name__}')
        print(f'Scopes value: {scopes}')
"
```

**期望输出**：
```
Has api_keys: True
Has protocols: True
API Keys count: 1
Scopes type: list
Scopes value: ['orders:read', 'products:read', 'orders:write']
```

### 3. 浏览器开发者工具检查

**F12 → Console**，输入：
```javascript
// 检查当前加载的组件版本
console.log(window.location.href);
// 强制重新加载，不使用缓存
location.reload(true);
```

---

## ✅ 完成清单

- [ ] Vercel 部署状态为 "Ready"
- [ ] 强制刷新浏览器（Cmd+Shift+R）
- [ ] Railway Redeploy（修复 scopes 格式，commit: 1a625f21）
- [ ] 等待 2-3 分钟
- [ ] 再次刷新查看

---

**如果 Vercel 已显示 Ready 但前端无变化，请清空浏览器缓存或使用无痕模式访问。**


## 前端没有变化的原因

### 可能原因 1: Vercel 还没部署完成 ⏳

**检查方法**：
1. 登录 Vercel Dashboard
2. 查看 `pivota-employee-portal` 项目
3. 检查 Deployments 页面
4. 最新部署应该是 commit `e6f5a68`
5. 状态应该是 "Ready"（绿色）

### 可能原因 2: 浏览器缓存 🔄

**解决方法**：
1. 打开 https://employee.pivota.cc/dashboard/agents
2. 打开开发者工具（F12）
3. 右键点击刷新按钮
4. 选择 "清空缓存并硬性重新加载"
5. 或按 `Cmd+Shift+R`（Mac）/ `Ctrl+Shift+F5`（Windows）

### 可能原因 3: 需要手动触发部署

**如果 Vercel 没有自动部署**：
1. Vercel Dashboard → pivota-employee-portal 项目
2. Deployments → 最新的 commit
3. 点击 "Redeploy"

---

## 验证前端代码已更新

### 方法 1: 检查浏览器控制台

打开 https://employee.pivota.cc/dashboard/agents

**F12 → Console**，查找：
```
🔧 [API Client] Initializing with baseURL: https://...
```

### 方法 2: 检查页面源码

**F12 → Sources → webpack**

搜索文件：`AgentDetailPanel`

应该能找到包含 "API Keys" 和 "Supported Protocols" 的代码。

### 方法 3: 测试 API 调用

打开详情弹窗，**F12 → Network**

应该看到：
```
GET /employee/agents/agent_ee38f2b3645a2ec2
Response: {
  "agent": {
    "api_keys": [...],
    "protocols": [...]
  }
}
```

如果 response 有 api_keys 和 protocols，但 UI 不显示：
- 说明前端代码还没更新
- 需要等待 Vercel 部署或强制刷新

---

## 预期的 UI 变化

### 详情弹窗中应该新增两个部分：

#### 在 "Governance Policies" 之后

**1. API Keys Section**（蓝色背景）
```
┌─────────────────────────────────────┐
│ 🔑 API Keys (1)                     │
│ ┌─────────────────────────────────┐ │
│ │ ak_live_886c...  [Active]       │ │
│ │ Scopes: orders:read, products:  │ │
│ │         read, orders:write      │ │
│ │ Created: 10/27/2025, ...        │ │
│ └─────────────────────────────────┘ │
│ Note: Generate, revoke, and rotate │
│ keys via API endpoints             │
│ (UI coming in Phase 3)             │
└─────────────────────────────────────┘
```

**2. Protocols Section**（紫色背景）
```
┌─────────────────────────────────────┐
│ 🔌 Supported Protocols (1)          │
│ [REST v1.0] (绿色 badge)            │
│ Protocol management UI coming in   │
│ Phase 3                            │
└─────────────────────────────────────┘
```

---

## 故障排除

### 问题：强制刷新后仍无变化

**检查 Vercel 部署**：
```bash
# 检查最新部署时间
# 应该是几分钟前
```

**手动触发部署**：
1. Vercel Dashboard
2. pivota-employee-portal 项目
3. Deployments 标签
4. 点击 "Redeploy"
5. 选择最新 commit（e6f5a68）

### 问题：UI 显示但数据格式错误

**刚修复了 scopes 格式问题**（Commit: 1a625f21）

需要：
1. Railway Redeploy（等待 2-3 分钟）
2. 刷新前端查看

**正确的 scopes 显示**：
```
Scopes: orders:read, products:read, orders:write
```

**不应该是**：
```
Scopes: ["orders:read", "products:read", "orders:write"]
```

---

## 调试步骤

### 1. 确认 Vercel 部署状态

Vercel Dashboard 应该显示：
- Latest Deployment: e6f5a68
- Status: Ready
- Time: 最近几分钟

### 2. 确认后端返回正确数据

```bash
curl -sS 'https://web-production-fedb.up.railway.app/employee/agents/agent_ee38f2b3645a2ec2' \
  -H 'Authorization: Bearer YOUR_TOKEN' | python3 -c "
import sys, json
data = json.load(sys.stdin)
agent = data.get('agent', {})
print(f'Has api_keys: {\"api_keys\" in agent}')
print(f'Has protocols: {\"protocols\" in agent}')
if 'api_keys' in agent:
    print(f'API Keys count: {len(agent[\"api_keys\"])}')
    if agent['api_keys']:
        scopes = agent['api_keys'][0].get('scopes')
        print(f'Scopes type: {type(scopes).__name__}')
        print(f'Scopes value: {scopes}')
"
```

**期望输出**：
```
Has api_keys: True
Has protocols: True
API Keys count: 1
Scopes type: list
Scopes value: ['orders:read', 'products:read', 'orders:write']
```

### 3. 浏览器开发者工具检查

**F12 → Console**，输入：
```javascript
// 检查当前加载的组件版本
console.log(window.location.href);
// 强制重新加载，不使用缓存
location.reload(true);
```

---

## ✅ 完成清单

- [ ] Vercel 部署状态为 "Ready"
- [ ] 强制刷新浏览器（Cmd+Shift+R）
- [ ] Railway Redeploy（修复 scopes 格式，commit: 1a625f21）
- [ ] 等待 2-3 分钟
- [ ] 再次刷新查看

---

**如果 Vercel 已显示 Ready 但前端无变化，请清空浏览器缓存或使用无痕模式访问。**

