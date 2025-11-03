# Agent Portal 登录问题修复指南

## 🔴 问题症状
```
401 Unauthorized 错误：
- /agents/{agent_id}/merchants → 401
- /agents/{agent_id}/revenue/expectations → 401
- /agents/{agent_id}/settlements → 401
```

## 🎯 根本原因
Agent Portal 之前使用**假的 mock token**，后端无法验证，导致所有 API 调用失败。

## ✅ 解决方案

### 方案 1: 创建真实的 Agent 账户（推荐）

#### 步骤 1: 在 Railway 数据库中创建 agent 用户

访问 Railway → 你的项目 → PostgreSQL → Query

执行以下 SQL：

```sql
-- 1. 检查是否已存在
SELECT id, email, role, active FROM users WHERE email = 'agent@pivota.com';

-- 2. 如果不存在，创建 agent 用户
INSERT INTO users (email, password_hash, full_name, role, active, created_at)
VALUES (
  'agent@pivota.com',
  -- Password: Agent123456 (bcrypt hash)
  '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5ViT0VrKZOBBu',
  'Pivota Agent',
  'agent',
  true,
  NOW()
)
ON CONFLICT (email) DO NOTHING
RETURNING id, email, role;

-- 3. 验证创建成功
SELECT 
  u.id, 
  u.email, 
  u.role, 
  u.active,
  a.agent_id,
  a.name as agent_name
FROM users u
LEFT JOIN agents a ON u.email = a.email
WHERE u.email = 'agent@pivota.com';
```

#### 步骤 2: 清除旧的假 token

在浏览器中打开 **Agent Portal** (https://agents.pivota.cc/)：

1. 按 **F12** 打开开发者工具
2. 进入 **Application** 或 **存储** 标签
3. 点击 **Storage** → **Clear site data**
4. 或手动删除 localStorage 中的项：
   ```javascript
   localStorage.removeItem('agent_token');
   localStorage.removeItem('agent_user');
   localStorage.removeItem('agent_id');
   localStorage.removeItem('agent_api_key');
   ```

#### 步骤 3: 重新登录

访问: https://agents.pivota.cc/login

使用凭据：
- **Email**: `agent@pivota.com`
- **Password**: `Agent123456`

登录成功后，检查 localStorage（在控制台运行）：
```javascript
console.log('Token:', localStorage.getItem('agent_token'));
console.log('Agent ID:', localStorage.getItem('agent_id'));
```

✅ 应该看到真实的 JWT token（以 `eyJ` 开头）

#### 步骤 4: 验证数据

访问以下页面验证数据是否正常：
- `/dashboard` - 应该显示真实的 metrics
- `/merchants` - 应该显示授权的商户列表
- `/orders` - 应该显示该 agent 创建的订单
- `/revenue` - 应该显示收益和结算信息

---

### 方案 2: 使用脚本自动创建（如果你有 Employee Token）

如果你有 Employee Portal 的 token，可以运行脚本：

```bash
# 1. 获取 Employee Token
# 访问 https://employee.pivota.cc/
# 登录后在控制台运行: localStorage.getItem('employee_token')

# 2. 运行脚本
./create_agent_portal_user.sh <YOUR_EMPLOYEE_TOKEN>
```

---

## 🧪 验证 API 是否工作

登录成功后，在浏览器控制台测试：

```javascript
// 测试 Merchants API
fetch('https://web-production-fedb.up.railway.app/agents/agent_ee38f2b3645a2ec2/merchants', {
  headers: {
    'Authorization': 'Bearer ' + localStorage.getItem('agent_token')
  }
})
.then(r => r.json())
.then(d => console.log('Merchants:', d));

// 测试 Revenue API
fetch('https://web-production-fedb.up.railway.app/agents/agent_ee38f2b3645a2ec2/revenue/expectations', {
  headers: {
    'Authorization': 'Bearer ' + localStorage.getItem('agent_token')
  }
})
.then(r => r.json())
.then(d => console.log('Revenue Expectations:', d));
```

✅ 如果返回数据而不是 401，说明认证成功！

---

## 📊 Phase 5.6 Revenue 页面验证清单

登录成功并有真实 token 后，验证以下功能：

### Revenue Dashboard (`/revenue`)

#### 1. Earnings Summary（收益概览）
- [ ] **Total Earned (30d)** - 显示数字或 $0.00
- [ ] **Pending Settlement** - 显示待结算金额
- [ ] **Settled** - 显示已结算金额

#### 2. Revenue Expectations（如果已设置）
- [ ] **Expected Rate** - 期望佣金比例 (%)
- [ ] **Minimum Acceptable Rate** - 最低可接受比例 (%)
- [ ] 紫色渐变卡片展示

#### 3. Settlement History
- [ ] 显示结算记录列表（如果有）
- [ ] 每条记录包含：
  - Settlement ID
  - 交易数量
  - 金额
  - 状态
- [ ] 或显示空状态提示

---

## 🔧 如果仍然有问题

### 检查 users 表是否有对应的 agent 记录

```sql
SELECT 
  u.id as user_id,
  u.email,
  u.role,
  u.active,
  a.agent_id,
  a.name as agent_name,
  a.status
FROM users u
LEFT JOIN agents a ON u.email = a.email
WHERE u.email = 'agent@pivota.com' OR a.agent_id = 'agent_ee38f2b3645a2ec2';
```

应该返回：
- `role = 'agent'`
- `active = true`
- `agent_id = 'agent_ee38f2b3645a2ec2'`

如果 users 表中没有记录，执行方案 1 中的 SQL 创建。

---

## 🎉 修复完成后

所有页面应该显示真实数据：
- ✅ Dashboard - Metrics 和 Activity
- ✅ Merchants - 授权商户列表
- ✅ Orders - 订单列表
- ✅ Revenue - 收益和结算
- ✅ Analytics - 详细分析

**不再是全是 0 的占位数据！** 🎊

