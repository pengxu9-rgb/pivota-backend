# 🔗 Vercel 重新连接 GitHub - 操作指南

## ✅ GitHub 推送成功

三个项目已推送到 GitHub：

1. **Agent Portal**: https://github.com/pengxu9-rgb/pivota-agents-portal
2. **Merchant Portal**: https://github.com/pengxu9-rgb/pivota-merchants-portal
3. **Employee Portal**: https://github.com/pengxu9-rgb/pivota-employee-portal

---

## 🎯 在 Vercel 连接 GitHub 仓库

### 推荐方式：删除现有项目，重新导入（最干净）

#### Step 1: 删除现有项目（可选备份）

访问 https://vercel.com/dashboard

对于每个项目：
1. 进入项目 Settings
2. 滚动到底部 "Delete Project"
3. 输入项目名称确认删除

**需要删除的项目：**
- `pivota-agents-portal`
- `pivota-merchants-portal`
- `pivota-employee-portal`

#### Step 2: 从 GitHub 重新导入

1. 点击 **"Add New Project"**
2. 选择 **"Import Git Repository"**
3. 如果没看到仓库，点击 "Adjust GitHub App Permissions"
4. 选择三个仓库：
   - `pivota-agents-portal`
   - `pivota-merchants-portal`
   - `pivota-employee-portal`

#### Step 3: 配置每个项目

**对于 pivota-agents-portal：**
```
Framework Preset: Next.js (自动识别)

Environment Variables:
NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
NEXT_PUBLIC_SITE_URL=https://agents.pivota.cc
NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc

点击 "Deploy"
```

**对于 pivota-merchants-portal：**
```
Framework Preset: Next.js

Environment Variables:
NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
NEXT_PUBLIC_SITE_URL=https://merchants.pivota.cc
NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc

点击 "Deploy"
```

**对于 pivota-employee-portal：**
```
Framework Preset: Next.js

Environment Variables:
NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
NEXT_PUBLIC_SITE_URL=https://employee.pivota.cc
NEXT_PUBLIC_MARKETING_SITE=https://pivota.cc

点击 "Deploy"
```

#### Step 4: 部署完成后添加域名

每个项目部署成功后：
1. 进入 Project Settings
2. 点击 "Domains"
3. 添加对应域名：
   - Agent Portal → `agents.pivota.cc`
   - Merchant Portal → `merchants.pivota.cc`
   - Employee Portal → `employee.pivota.cc`

---

## 🔄 或者：保持现有项目，添加 Git 连接

如果你想保留现有的 Vercel 项目：

### 对于每个项目：

1. 进入项目 Settings
2. 点击 "Git" 标签
3. 点击 "Connect Git Repository"
4. 选择 GitHub
5. 选择对应的仓库
6. Production Branch: `main`
7. 保存

然后手动触发一次 Redeploy。

---

## ✅ 连接成功后的效果

### 自动部署工作流：

```bash
# 1. 修改代码
cd pivota-agents-portal
# 编辑文件...

# 2. 提交并推送
git add .
git commit -m "Update feature"
git push

# 3. Vercel 自动部署 ✨
# 1-2 分钟后 https://agents.pivota.cc 自动更新
```

### 在 Vercel Dashboard 可以看到：

- ✅ 每次 Git 推送的部署历史
- ✅ 代码变更对比
- ✅ 部署日志
- ✅ 一键回滚功能

---

## 🎊 最终架构

```
GitHub Repositories (代码源)
├── pivota-agents-portal
├── pivota-merchants-portal
└── pivota-employee-portal
     ↓ (自动触发)
Vercel 部署
├── agents.pivota.cc
├── merchants.pivota.cc
└── employee.pivota.cc
     ↓ (访问)
用户访问网站
```

---

## 📋 快速操作清单

- [x] 在 GitHub 创建三个仓库
- [x] 推送代码到 GitHub
- [ ] 在 Vercel 重新导入项目（或连接 Git）
- [ ] 配置环境变量
- [ ] 添加自定义域名
- [ ] 验证自动部署工作正常

---

**现在请访问 https://vercel.com/dashboard 并从 GitHub 重新导入这三个项目！** 🚀
