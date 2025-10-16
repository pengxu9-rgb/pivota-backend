# 🔑 管理员账号信息

## 当前问题

数据库中的旧账号可能已失效。让我们创建一个新的管理员账号。

## 🆕 创建新管理员账号

### 方法1: 通过前端注册（推荐）

1. **访问**: `http://localhost:3000/login`
2. **点击** "Sign Up" 标签
3. **填写信息**:
   ```
   Email: youremail@example.com
   Password: YourPassword123!
   Role: admin
   ```
4. **提交** - 系统会自动批准admin角色
5. **登录** - 使用相同的邮箱和密码

### 方法2: 通过API创建

```bash
curl -X POST https://pivota-dashboard.onrender.com/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "youremail@example.com",
    "password": "YourPassword123!",
    "role": "admin"
  }'
```

**返回**:
```json
{
  "status": "success",
  "message": "Account created and approved!",
  "user_id": "...",
  "role": "admin",
  "approved": true
}
```

## 🔍 测试登录

### 前端:
访问 `http://localhost:3000/login` 并使用你创建的账号。

### API:
```bash
curl -X POST https://pivota-dashboard.onrender.com/auth/signin \
  -H "Content-Type: application/json" \
  -d '{
    "email": "youremail@example.com",
    "password": "YourPassword123!"
  }'
```

## ⚠️ 已知问题

如果登录失败显示 "Invalid credentials"，可能的原因：

1. **Supabase同步问题** - Backend创建了user但Supabase auth没有同步
2. **密码加密问题** - 密码可能未正确存储
3. **数据库重置** - Render重启可能清空了SQLite数据

## 💡 解决方案

### 快速解决：使用前端注册

前端会直接调用后端API，更可靠：

1. 打开浏览器
2. 访问 `http://localhost:3000/login`
3. 切换到 "Sign Up"
4. 填写表单并提交
5. 应该会立即批准（admin角色）
6. 然后用相同账号登录

### 检查是否成功

登录后，你应该能看到：
- ✅ 重定向到 `/admin`
- ✅ 看到 "Admin Dashboard"
- ✅ 看到 "Onboarding (Phase 2)" 标签

## 📝 当前系统状态

- ✅ 后端API正常工作
- ✅ 前端界面正常加载
- ✅ 商户入驻功能可用（不需要登录）
- ⚠️ 管理员账号需要重新创建

## 🎯 两个选项

### Option A: 测试商户入驻（不需要登录）

访问 `http://localhost:3000/merchant/onboarding`
- 可以直接注册商户
- 测试KYC流程
- 连接PSP
- 不需要管理员账号

### Option B: 创建管理员账号

使用上面的方法创建新管理员，然后：
- 查看所有入驻的商户
- 手动批准/拒绝
- 查看系统统计

---

**建议**: 先测试 Option A（商户入驻），这是Phase 2的核心功能。管理员界面可以之后再测试。

