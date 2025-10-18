# ✅ 最终部署清单

## 🎯 所有准备工作已完成！

### ✅ 已完成的工作

- [x] 后端 API 部署（Railway）
- [x] 新鉴权系统上线
- [x] 测试账号密码修复
- [x] DNS 配置完成（agents/merchants/employee.pivota.cc）
- [x] 宣传页面更新（两个入口按钮）
- [x] 三个前端项目创建并配置
- [x] 登录/注册页面完成
- [x] Employee Dashboard 完成
- [x] Merchant Onboarding 流程集成
- [x] Agent Integration 页面完成

---

## 🚀 立即部署步骤

### Step 1: 在 GitHub 创建三个仓库

访问：https://github.com/new

创建以下三个仓库（设为 Public 或 Private）：
```
1. pivota-agents-portal
2. pivota-merchants-portal
3. pivota-employee-portal
```

### Step 2: 推送代码到 GitHub

```bash
# Agent Portal
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-agents-portal
git init
git add -A
git commit -m "Initial commit: Agent Portal"
git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git
git branch -M main
git push -u origin main

# Merchant Portal
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal
git init
git add -A
git commit -m "Initial commit: Merchant Portal"
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
git branch -M main
git push -u origin main

# Employee Portal
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-employee-portal
git init
git add -A
git commit -m "Initial commit: Employee Portal"
git remote add origin https://github.com/pengxu9-rgb/pivota-employee-portal.git
git branch -M main
git push -u origin main
```

### Step 3: 在 Vercel 导入项目

访问：https://vercel.com/new

为每个项目：

#### 3.1 Agent Portal
```
1. 点击 "Import Git Repository"
2. 选择 "pivota-agents-portal"
3. Framework Preset: Next.js (自动)
4. 环境变量添加：
   NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
   NEXT_PUBLIC_SITE_URL=https://agents.pivota.cc
   NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc
5. 点击 "Deploy"
6. 部署成功后，Settings → Domains → 添加 "agents.pivota.cc"
```

#### 3.2 Merchant Portal
```
重复上述步骤，但使用：
- 仓库: pivota-merchants-portal
- NEXT_PUBLIC_SITE_URL=https://merchants.pivota.cc
- 域名: merchants.pivota.cc
```

#### 3.3 Employee Portal
```
重复上述步骤，但使用：
- 仓库: pivota-employee-portal
- NEXT_PUBLIC_SITE_URL=https://employee.pivota.cc
- 域名: employee.pivota.cc
```

---

## 🔍 DNS 验证（已完成）

你的 DNS 已正确配置：

```bash
✅ agents.pivota.cc → cname.vercel-dns.com
✅ merchants.pivota.cc → cname.vercel-dns.com
✅ employee.pivota.cc → cname.vercel-dns.com
```

Vercel 会自动识别这些域名！

---

## 📊 项目功能总结

### **Agent Portal** (agents.pivota.cc)

#### 登录/注册
- `/login` - Agent 登录页面
- `/signup` - Agent 注册页面

#### 核心功能
- `/dashboard` - 显示 API 调用数、订单数、GMV、成功率
- `/integration` - **真实可用的 MCP/API 集成**
  - ✅ API Key 管理
  - ✅ Python/Node.js 代码示例
  - ✅ cURL 测试命令
  - ✅ 完整的 API 文档链接
  - ✅ SDK 下载
  - ✅ 6个核心 API Endpoints

#### 测试账号
```
email: agent@test.com
password: Admin123!
```

---

### **Merchant Portal** (merchants.pivota.cc)

#### 登录/注册
- `/login` - Merchant 登录页面
- `/signup` - **完整的 Onboarding 流程**
  - Step 1: Business Registration
  - Step 2: PSP Setup (Stripe/Adyen)
  - Step 3: Document Upload (KYB)
  - Step 4: Completion (API Key 发放)

#### 测试账号
```
email: merchant@test.com
password: Admin123!
```

---

### **Employee Portal** (employee.pivota.cc)

#### 登录
- `/login` - Employee 登录页面（内部使用）

#### 核心功能
- `/dashboard` - 员工管理界面
  - **Merchants Tab**: 审核、文档上传、店铺连接（三点菜单）
  - **Agents Tab**: 查看、重置密钥、停用
  - **Analytics Tab**: 平台总览数据
  - **System Tab**: PSP 状态、系统健康

#### 测试账号
```
email: employee@pivota.com
password: Admin123!

或：
email: admin@pivota.com
password: Admin123!
```

---

## 🌐 完整的用户流程

### For Agents (AI 开发者)
```
1. 访问 https://pivota.cc
2. 点击 "For AI Agents" 卡片
3. 跳转到 https://agents.pivota.cc/signup
4. 注册账号
5. 查看 /integration 页面，获取 API Key
6. 下载 SDK，开始集成
7. 测试 API 调用
8. 查看 /dashboard 监控性能
```

### For Merchants (商家)
```
1. 访问 https://pivota.cc
2. 点击 "For Merchants" 卡片
3. 跳转到 https://merchants.pivota.cc/signup
4. 进入 Onboarding 流程：
   a. 填写商业信息
   b. 配置支付（Stripe/Adyen）
   c. 上传 KYB 文档
   d. 获取 API 凭证
5. 等待审核（自动/手动）
6. 连接店铺（Shopify/Wix）
7. 开始接收订单
```

### For Employees (内部员工)
```
1. 直接访问 https://employee.pivota.cc
2. 使用内部账号登录
3. 管理商户和 Agents
4. 审核文档
5. 监控系统状态
```

---

## 📝 Vercel 环境变量

每个项目需要配置以下环境变量：

### Agent Portal
```env
NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
NEXT_PUBLIC_SITE_URL=https://agents.pivota.cc
NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc
```

### Merchant Portal
```env
NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
NEXT_PUBLIC_SITE_URL=https://merchants.pivota.cc
NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc
```

### Employee Portal
```env
NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
NEXT_PUBLIC_SITE_URL=https://employee.pivota.cc
NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc
```

---

## ✅ 最终验证清单

部署完成后，逐一验证：

### 主页测试
- [ ] 访问 https://pivota.cc
- [ ] 点击 "For AI Agents" 按钮 → 跳转到 agents.pivota.cc/signup
- [ ] 点击 "For Merchants" 按钮 → 跳转到 merchants.pivota.cc/signup
- [ ] Header 的 "Login" 按钮保留（通用登录入口）

### Agent Portal 测试
- [ ] https://agents.pivota.cc/login - 登录页正常
- [ ] 使用 agent@test.com 登录成功
- [ ] /dashboard 显示统计数据
- [ ] /integration 显示 API 文档和代码示例

### Merchant Portal 测试
- [ ] https://merchants.pivota.cc/login - 登录页正常
- [ ] /signup - Onboarding 流程完整（4 步骤）
- [ ] 使用 merchant@test.com 登录成功

### Employee Portal 测试
- [ ] https://employee.pivota.cc/login - 登录页正常
- [ ] 使用 employee@pivota.com 登录成功
- [ ] /dashboard 显示 Merchants、Agents、Analytics、System 四个标签
- [ ] 商户的三点菜单功能正常

### SSL 验证
- [ ] 所有域名都使用 HTTPS
- [ ] SSL 证书有效（Let's Encrypt）
- [ ] 无安全警告

---

## 🎉 完成后的效果

```
用户旅程
──────────────────────────────────────────────

访问 pivota.cc (宣传主页)
    ↓
看到两个精美的卡片：
- For AI Agents (紫色)
- For Merchants (蓝色)
    ↓
点击对应按钮
    ↓
跳转到专属门户注册/登录
    ↓
完成注册/登录
    ↓
进入专属 Dashboard
    ↓
开始使用 Pivota 服务
```

---

## 💡 推广策略

### Agent Portal 推广点
- ✅ "Turn Any AI into a Commerce Agent"
- ✅ 统一 API，接入多商户
- ✅ 完整 SDK（Python/Node.js）
- ✅ MCP Server 支持
- ✅ 真实可用的测试环境

### Merchant Portal 推广点
- ✅ "Open Your Store to the AI Economy"
- ✅ 5分钟完成 Onboarding
- ✅ 自动化审核系统
- ✅ 多 PSP 支持（Stripe/Adyen）
- ✅ Shopify/Wix 一键集成

---

## 🔧 下一步优化建议

1. **Agent Portal 增强**（可选）
   - 添加 API Playground（实时测试 API）
   - Webhook 配置界面
   - 更详细的 Analytics 图表

2. **Merchant Portal 增强**（可选）
   - 产品管理界面
   - 订单管理界面
   - 收入报表

3. **营销优化**（可选）
   - SEO 优化
   - 添加演示视频
   - 客户案例展示

---

**准备好了！所有代码和配置都已完成。现在只需要推送到 GitHub 并在 Vercel 导入即可！** 🚀

DNS 已经配置好，等 Vercel 部署完成后，所有链接就会自动生效！
