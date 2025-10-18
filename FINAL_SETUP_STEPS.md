# ✅ 最终设置步骤 - 在 Vercel 重新导入项目

## 🎊 代码已全部推送到 GitHub

所有三个项目都已在 GitHub 上：
- ✅ https://github.com/pengxu9-rgb/pivota-agents-portal
- ✅ https://github.com/pengxu9-rgb/pivota-merchants-portal
- ✅ https://github.com/pengxu9-rgb/pivota-employee-portal

---

## 🔧 在 Vercel Dashboard 的操作

### Step 1: 删除旧的直接部署项目

访问：https://vercel.com/dashboard

删除以下项目（它们是通过 CLI 直接上传的）：
1. `pivota-agents-portal`
2. `pivota-merchants-portal`
3. `pivota-employee-portal`

**如何删除：**
- 进入项目 → Settings → 滚动到底部
- "Delete Project" → 输入项目名确认

---

### Step 2: 从 GitHub 重新导入三个项目

#### 2.1 导入 Agent Portal

1. 点击 **"Add New Project"**
2. 选择 **"Import Git Repository"**
3. 找到 `pengxu9-rgb/pivota-agents-portal`
4. 点击 "Import"
5. 配置页面：
   ```
   Project Name: pivota-agents-portal
   Framework Preset: Next.js (自动识别)
   Root Directory: ./
   
   Environment Variables:
   NEXT_PUBLIC_API_URL = https://web-production-fedb.up.railway.app
   NEXT_PUBLIC_SITE_URL = https://agents.pivota.cc
   NEXT_PUBLIC_MARKETING_SITE = https://pivota.cc
   ```
6. 点击 **"Deploy"**
7. 等待部署完成（1-2分钟）
8. 进入 Settings → Domains
9. 添加域名：`agents.pivota.cc`
10. 验证 DNS（应该自动通过，因为你已配置好）

#### 2.2 导入 Merchant Portal

重复上述步骤，但使用：
- 仓库：`pivota-merchants-portal`
- 域名：`merchants.pivota.cc`
- 环境变量中的 `NEXT_PUBLIC_SITE_URL = https://merchants.pivota.cc`

#### 2.3 导入 Employee Portal

重复上述步骤，但使用：
- 仓库：`pivota-employee-portal`
- 域名：`employee.pivota.cc`
- 环境变量中的 `NEXT_PUBLIC_SITE_URL = https://employee.pivota.cc`

---

## ⚡ 更快的方式

Vercel 会自动读取 `vercel.json` 中的环境变量配置，所以你可以：

1. Import 项目时直接点击 "Deploy"（不用手动添加环境变量）
2. 部署成功后再添加自定义域名

---

## 🧪 部署完成后验证

### 检查所有链接：

```bash
# 1. Agent Portal
curl -I https://agents.pivota.cc

# 2. Merchant Portal
curl -I https://merchants.pivota.cc

# 3. Employee Portal
curl -I https://employee.pivota.cc

# 4. 宣传主页
curl -I https://pivota.cc
```

所有应该返回 `HTTP/2 200`

---

## 📱 浏览器测试

### 1. 主页入口测试
- 访问：https://pivota.cc
- 应该看到两个卡片：
  - "For AI Agents" 卡片
  - "For Merchants" 卡片
- 点击 "Start Building" → 跳转到 agents.pivota.cc/signup
- 点击 "Get Started" → 跳转到 merchants.pivota.cc/signup

### 2. Agent Portal 测试
- 访问：https://agents.pivota.cc/login
- 使用：agent@test.com / Admin123!
- 登录后查看 Dashboard
- 访问：/integration 查看 MCP/API 文档

### 3. Merchant Portal 测试
- 访问：https://merchants.pivota.cc/login
- 使用：merchant@test.com / Admin123!
- 或访问：/signup 测试 Onboarding 流程

### 4. Employee Portal 测试
- 访问：https://employee.pivota.cc/login
- 使用：employee@pivota.com / Admin123!
- 查看 Merchants/Agents 管理功能

---

## 🎯 自动部署验证

重新导入后，测试自动部署：

```bash
# 1. 修改任意项目代码
cd pivota-agents-portal
echo "// test auto-deploy" >> src/app/page.tsx

# 2. 提交并推送
git add .
git commit -m "Test auto-deploy"
git push

# 3. 观察 Vercel Dashboard
# 应该会自动创建新的部署
# 约 1-2 分钟完成
```

---

## ✅ 完成后的效果

### GitHub → Vercel 自动部署流程：

```
本地代码修改
    ↓
git commit & push
    ↓
GitHub 仓库更新
    ↓
Vercel 自动检测
    ↓
自动构建和部署
    ↓
网站自动更新 (1-2分钟)
```

---

## 📊 最终系统架构

```
GitHub 代码仓库
├── pivota-agents-portal (main)
├── pivota-merchants-portal (main)
├── pivota-employee-portal (main)
└── pivota-ai-flow-26 (lovable-website)
     ↓ (自动部署)
Vercel 托管
├── agents.pivota.cc
├── merchants.pivota.cc
├── employee.pivota.cc
└── pivota.cc
     ↓ (API 调用)
Railway 后端
└── web-production-fedb.up.railway.app
```

---

## 🚀 下一步

访问 **https://vercel.com/dashboard** 并：

1. 删除三个旧项目（CLI 直接上传的）
2. 重新从 GitHub 导入
3. 添加域名
4. 测试所有功能

**大约 10-15 分钟完成全部设置！** 🎯
