# 🚂 Railway 部署指南

## ✅ 准备工作
- ✅ Railway 账号已创建
- ✅ GitHub 仓库已准备好
- ✅ 配置文件已添加（`railway.json`, `nixpacks.toml`）

---

## 📋 部署步骤

### 1️⃣ 创建新项目

1. 登录 Railway Dashboard: https://railway.app/dashboard
2. 点击 **"New Project"**
3. 选择 **"Deploy from GitHub repo"**
4. 授权 Railway 访问你的 GitHub（如果还没有）
5. 选择你的仓库：`pivota-dashboard-1760371224`
6. 点击 **"Deploy Now"**

---

### 2️⃣ 添加 PostgreSQL 数据库

1. 在项目页面，点击 **"New"** → **"Database"** → **"Add PostgreSQL"**
2. Railway 会自动：
   - 创建 PostgreSQL 数据库
   - 生成 `DATABASE_URL` 环境变量
   - 将其链接到你的应用

**重要**: Railway 的 PostgreSQL 是**原生的**，没有 pgbouncer，所以：
- ✅ 支持 prepared statements
- ✅ 零配置问题
- ✅ 完美兼容 asyncpg

---

### 3️⃣ 配置环境变量

在项目中，点击你的应用服务 → **"Variables"** 标签：

#### 必需的环境变量：

```bash
# JWT Secret（生成一个随机字符串）
JWT_SECRET_KEY=<生成一个强随机字符串>

# Stripe（从你的 Stripe Dashboard 获取）
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Adyen（从你的 Adyen Dashboard 获取）
ADYEN_API_KEY=AQE...
ADYEN_MERCHANT_ACCOUNT=WoopayECOM
```

#### 可选的环境变量（如果有）：

```bash
# Legacy Shopify
SHOPIFY_ACCESS_TOKEN=shpat_...
SHOPIFY_STORE_URL=https://yourstore.myshopify.com

# Legacy Wix
WIX_API_KEY=...
WIX_STORE_URL=...

# Supabase（如果用于用户管理）
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
```

**注意**: `DATABASE_URL` 会由 PostgreSQL 插件自动设置，**不需要**手动添加。

---

### 4️⃣ 触发部署

1. 环境变量配置完成后，点击 **"Deploy"** 或等待自动部署
2. Railway 会：
   - 安装依赖（从 `requirements.txt`）
   - 构建应用
   - 启动服务
   - 运行健康检查

---

### 5️⃣ 查看部署状态

1. 在 **"Deployments"** 标签查看构建日志
2. 等待状态变为 **"Active"**（绿色）
3. 点击 **"View Logs"** 查看运行日志

---

### 6️⃣ 获取应用 URL

1. 在项目页面，点击 **"Settings"** → **"Networking"**
2. 点击 **"Generate Domain"**
3. Railway 会生成一个公开 URL，例如：
   ```
   https://pivota-dashboard-production.up.railway.app
   ```
4. 复制这个 URL 用于访问你的应用

---

## 🧪 测试部署

### 1. 测试健康检查
```bash
curl https://your-app.up.railway.app/
```

应该返回：
```json
{
  "message": "Pivota Infrastructure Dashboard API",
  "version": "0.2",
  "status": "healthy",
  "db_status": "connected",
  ...
}
```

### 2. 测试管理员登录
访问：
```
https://your-app.up.railway.app/admin/dashboard
```

使用凭据：
- Email: `superadmin@pivota.com`
- Password: `admin123`

### 3. 测试商户注册
访问商户入驻门户：
```
https://your-app.up.railway.app/merchant/onboarding/portal
```

---

## 🔍 故障排除

### 部署失败？

1. **查看构建日志**：
   - Deployments → 选择失败的部署 → View Logs
   - 查找红色的错误信息

2. **常见问题**：
   - **依赖安装失败**: 检查 `requirements.txt` 是否完整
   - **启动失败**: 检查 `nixpacks.toml` 的启动命令
   - **健康检查超时**: 增加 `healthcheckTimeout` 或检查启动逻辑

### 数据库连接失败？

1. **检查 `DATABASE_URL`**：
   - Variables 标签 → 确认 `DATABASE_URL` 存在
   - 应该类似：`postgresql://postgres:...@containers-us-west-xxx.railway.app:xxx/railway`

2. **检查数据库状态**：
   - 点击 PostgreSQL 服务 → 确认状态为 "Active"

### 环境变量问题？

1. **重新部署**：
   - 修改环境变量后，点击 **"Redeploy"** 强制重启

---

## 📊 监控和日志

### 实时日志
```bash
# 在项目页面
View Logs → 实时查看应用输出
```

### Metrics
- CPU 使用率
- 内存使用
- 网络流量
- 请求延迟

都在 **"Metrics"** 标签可见。

---

## 💰 费用管理

### 查看使用情况
1. 点击右上角头像 → **"Usage"**
2. 查看当前账单周期的使用额度
3. $5 免费额度通常够开发使用 1-2 个月

### 控制成本
- **开发期间**: 免费额度足够
- **生产环境**: 预计 $10-20/月（取决于流量）
- **暂停服务**: 可以随时暂停不用的服务

---

## 🚀 持续部署

Railway 已自动配置了 CI/CD：

1. **每次 `git push` 到 main 分支**，Railway 会自动部署
2. **无需手动操作**
3. **查看部署历史**: Deployments 标签

---

## 🎉 完成！

现在你的应用应该已经在 Railway 上稳定运行了！

**下一步**：
- 测试商户注册流程
- 测试 PSP 连接
- 开始开发 Shopify/Wix 集成功能

---

## 📞 需要帮助？

如果遇到问题，提供以下信息：
1. 部署日志的截图或文本
2. 错误信息的完整内容
3. 环境变量配置（隐藏敏感信息）

