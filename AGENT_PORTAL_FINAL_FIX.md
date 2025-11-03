# Agent Portal 最终修复 - 401 错误解决方案

## ✅ 好消息：后端已部署成功！

Railway 后端已经部署了新代码，JWT token 现在包含 `email` 字段：

```json
{
  "sub": "asdf@asdf.com",
  "email": "asdf@asdf.com",  ← 新添加
  "user_id": "...",
  "role": "agent",
  "agent_id": "agent_ee38f2b3645a2ec2",
  "exp": ...,
  "iat": ...
}
```

## 🔴 为什么仍然 401？

**原因**：你的浏览器缓存了**旧的 token**（没有 email 字段），后端无法验证旧 token 导致 401。

## ✅ 解决方案（2分钟修复）

### 步骤 1: 清除浏览器缓存

1. 打开 **Agent Portal**: https://agents.pivota.cc/
2. 按 **F12** 打开开发者工具
3. 进入 **Application** 标签（或 **应用程序** / **存储**）
4. 点击左侧 **Local Storage** → `https://agents.pivota.cc`
5. 点击 **Clear All** 或删除以下键：
   - `agent_token`
   - `agent_user`
   - `agent_id`
   - `agent_api_key`

**或者更简单的方法**：
在控制台（Console）运行：
```javascript
localStorage.clear();
console.log('✅ 已清除所有旧数据');
location.reload();
```

### 步骤 2: 重新登录

页面会自动跳转到 `/login`

使用你的凭据登录：
- **Email**: `asdf@asdf.com`
- **Password**: `Qwer1234`

### 步骤 3: 验证新 Token

登录成功后，在控制台（Console）运行：

```javascript
const token = localStorage.getItem('agent_token');
console.log('Token:', token);

// 解码 token 查看 payload
const payload = JSON.parse(atob(token.split('.')[1]));
console.log('Payload:', payload);

// 检查是否有 email 字段
if (payload.email) {
  console.log('✅ 新 token 包含 email 字段！');
} else {
  console.log('❌ 仍是旧 token，请刷新页面重试');
}
```

**期望输出**：
```javascript
Payload: {
  sub: "asdf@asdf.com",
  email: "asdf@asdf.com",  ← 应该看到这个
  user_id: "...",
  role: "agent",
  agent_id: "agent_ee38f2b3645a2ec2"
}
✅ 新 token 包含 email 字段！
```

### 步骤 4: 验证数据显示

访问以下页面，应该都能正常显示数据（不再是 401）：

- ✅ **/dashboard** - Metrics 和 Activity
- ✅ **/merchants** - 授权商户列表（1个商户）
- ✅ **/orders** - 订单列表（1个订单，$24.99）
- ✅ **/revenue** - 收益和结算数据

---

## 🧪 如果仍有问题

### 检查 Token 是否正确存储

```javascript
console.log('agent_token:', localStorage.getItem('agent_token'));
console.log('agent_id:', localStorage.getItem('agent_id'));
console.log('agent_user:', localStorage.getItem('agent_user'));
```

应该看到：
- `agent_token`: 以 `eyJ` 开头的长字符串
- `agent_id`: `agent_ee38f2b3645a2ec2`
- `agent_user`: JSON 字符串包含 agent 信息

### 手动测试 API

在控制台运行：

```javascript
const token = localStorage.getItem('agent_token');
const agentId = localStorage.getItem('agent_id');

// 测试 Merchants API
fetch(`https://web-production-fedb.up.railway.app/agents/${agentId}/merchants`, {
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  }
})
.then(r => r.json())
.then(d => console.log('Merchants:', d))
.catch(e => console.error('Error:', e));
```

**如果返回数据** → ✅ 后端正常，前端会自动修复  
**如果仍然 401** → 说明 token 仍是旧的，需要重新登录

---

## 📊 当前系统状态

### 后端 (Railway) ✅
- **提交**: be928b3b
- **状态**: 已部署
- **JWT Token**: ✅ 包含 email 字段
- **认证逻辑**: ✅ 使用 agent_id
- **语法检查**: ✅ 286 个文件全部通过

### 前端 (Vercel) ✅
- **提交**: 0b403a2
- **状态**: 已部署
- **API 连接**: ✅ 连接真实后端
- **登录逻辑**: ✅ 调用真实 API

### 问题
- ❌ 浏览器缓存了旧 token
- ✅ 清除 localStorage 即可解决

---

## 🎯 快速修复（1分钟）

**最快的方法**：

1. 访问：https://agents.pivota.cc/
2. 按 F12 → Console
3. 运行：
```javascript
localStorage.clear();
location.reload();
```
4. 重新登录：asdf@asdf.com / Qwer1234
5. 访问 /merchants 验证数据

**应该立即看到数据而不是 401！** 🎉

---

## 💡 为什么之前一直 401？

```
旧流程（导致 401）:
浏览器 → 旧 token (无 email) → 后端 get_current_user
                                    ↓
                            检查 payload 中的 email 字段
                                    ↓
                            ❌ 缺失 → "Invalid token payload" → 401

新流程（修复后）:
浏览器 → 新 token (有 email) → 后端 get_current_user
                                    ↓
                            检查 payload 中的 email 字段
                                    ↓
                            ✅ 存在 → 验证 agent_id → 200
```

**关键**：必须清除旧 token，重新登录获取新 token！

