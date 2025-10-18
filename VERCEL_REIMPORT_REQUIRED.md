# ⚠️ 重要：需要在 Vercel 重新导入项目

## 🔍 问题诊断

### 当前状态
- ✅ 所有代码都在 GitHub
- ❌ Vercel 项目是通过 CLI 直接上传的（没有 Git 连接）
- ❌ Git 推送无法触发 Vercel 自动部署

### GitHub 仓库（全部已推送）
1. ✅ https://github.com/pengxu9-rgb/pivota-agents-portal
2. ✅ https://github.com/pengxu9-rgb/pivota-merchants-portal  
3. ✅ https://github.com/pengxu9-rgb/pivota-employee-portal
4. ✅ https://github.com/pengxu9-rgb/pivota-marketing

---

## 🔧 解决方案：在 Vercel 重新导入

### Step 1: 删除 CLI 创建的项目

访问：https://vercel.com/dashboard

删除以下项目（它们是通过 CLI 创建的，没有 Git 连接）：
```
1. pivota-agents-portal
2. pivota-merchants-portal
3. pivota-employee-portal
4. pivota-marketing
```

**如何删除：**
- 点击项目 → Settings
- 滚动到最底部
- "Delete Project"
- 输入项目名称确认

---

### Step 2: 从 GitHub 重新导入四个项目

#### 2.1 导入 Marketing Site (pivota.cc)

1. 点击 **"Add New Project"**
2. 选择 **"Import Git Repository"**
3. 找到 `pengxu9-rgb/pivota-marketing`
4. 点击 **"Import"**
5. 配置：
   ```
   Framework: Next.js (自动)
   Root Directory: ./
   
   Environment Variables:
   NEXT_PUBLIC_API_URL = https://web-production-fedb.up.railway.app
   NEXT_PUBLIC_SITE_URL = https://pivota.cc
   ```
6. 点击 **"Deploy"**
7. 部署完成后：Settings → Domains → 添加 `pivota.cc` 和 `www.pivota.cc`

#### 2.2 导入 Agent Portal

重复上述步骤：
- 仓库：`pivota-agents-portal`
- 域名：`agents.pivota.cc`
- 环境变量：
  ```
  NEXT_PUBLIC_API_URL = https://web-production-fedb.up.railway.app
  NEXT_PUBLIC_SITE_URL = https://agents.pivota.cc
  NEXT_PUBLIC_MARKETING_SITE = https://pivota.cc
  ```

#### 2.3 导入 Merchant Portal

- 仓库：`pivota-merchants-portal`
- 域名：`merchants.pivota.cc`
- 环境变量：
  ```
  NEXT_PUBLIC_API_URL = https://web-production-fedb.up.railway.app
  NEXT_PUBLIC_SITE_URL = https://merchants.pivota.cc
  NEXT_PUBLIC_MARKETING_SITE = https://pivota.cc
  ```

#### 2.4 导入 Employee Portal

- 仓库：`pivota-employee-portal`
- 域名：`employee.pivota.cc`
- 环境变量：
  ```
  NEXT_PUBLIC_API_URL = https://web-production-fedb.up.railway.app
  NEXT_PUBLIC_SITE_URL = https://employee.pivota.cc
  NEXT_PUBLIC_MARKETING_SITE = https://pivota.cc
  ```

---

## ✅ 重新导入后的效果

### GitHub → Vercel 自动部署

```
本地修改代码
    ↓
git commit & push
    ↓
GitHub 仓库更新
    ↓
Vercel 自动检测 ✨
    ↓
自动构建和部署
    ↓
网站更新 (1-2分钟)
```

### 你会在 Vercel Dashboard 看到

- ✅ "Connected to GitHub" 标识
- ✅ 每次部署显示 Git commit 信息
- ✅ 可以直接从 GitHub commit 触发 Redeploy
- ✅ 代码变更对比

---

## 📋 操作清单

- [ ] 1. 访问 https://vercel.com/dashboard
- [ ] 2. 删除四个 CLI 创建的项目
- [ ] 3. 点击 "Add New Project"
- [ ] 4. 依次导入四个 GitHub 仓库
- [ ] 5. 配置环境变量
- [ ] 6. 部署
- [ ] 7. 添加自定义域名
- [ ] 8. 验证所有链接正常工作

---

## ⏱️ 预计时间

- 删除旧项目：2 分钟
- 重新导入四个项目：10-15 分钟
- 等待部署完成：5-10 分钟

**总共约 20-30 分钟完成全部设置**

---

## 💡 提示

Vercel 会自动读取每个项目根目录的 `vercel.json` 文件，所以环境变量可能会自动填充。但建议手动检查确保正确。

---

**准备好后开始删除并重新导入。完成后所有项目都会通过 GitHub 自动部署！** 🚀
