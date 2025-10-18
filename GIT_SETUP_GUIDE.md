# 🔄 Git Repository 设置指南

## 当前状态

你已经使用 Vercel CLI 直接部署了三个项目。现在建议连接 Git Repository 以实现自动部署。

---

## 🎯 **推荐方式：Git 自动部署**

### Step 1: 在 GitHub 创建三个仓库

访问：https://github.com/new

创建以下仓库（建议设为 Public）：
```
1. pivota-agents-portal
2. pivota-merchants-portal
3. pivota-employee-portal
```

### Step 2: 初始化并推送代码

```bash
# 1. Agent Portal
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-agents-portal
git init
git add -A
git commit -m "Initial commit: Agent Portal with MCP/API Integration"
git branch -M main
git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git
git push -u origin main

# 2. Merchant Portal
cd ../pivota-merchants-portal
git init
git add -A
git commit -m "Initial commit: Merchant Portal with Onboarding Flow"
git branch -M main
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
git push -u origin main

# 3. Employee Portal
cd ../pivota-employee-portal
git init
git add -A
git commit -m "Initial commit: Employee Portal for Internal Management"
git branch -M main
git remote add origin https://github.com/pengxu9-rgb/pivota-employee-portal.git
git push -u origin main
```

### Step 3: 在 Vercel 连接 Git Repository

#### 方式 A: Vercel Dashboard (推荐)

1. 访问 Vercel Dashboard: https://vercel.com/dashboard
2. 进入每个项目的 Settings
3. 找到 "Git Repository" 部分
4. 点击 "Connect Git Repository"
5. 选择对应的 GitHub 仓库
6. 点击 "Connect"

#### 方式 B: 删除并重新导入

1. 在 Vercel Dashboard 删除现有项目
2. 点击 "Add New Project"
3. 选择 "Import Git Repository"
4. 选择刚创建的三个仓库
5. 配置环境变量
6. 部署

---

## ⚡ **自动部署流程**

连接 Git 后，工作流程变成：

```bash
# 本地修改代码
cd pivota-agents-portal
# ... 编辑文件 ...

# 提交并推送
git add .
git commit -m "Update Agent Portal features"
git push

# Vercel 自动检测并部署 ✅
# 1-2分钟后，https://agents.pivota.cc 自动更新
```

---

## 🔧 **当前临时方案 vs Git 方案对比**

| 特性 | 当前方案 (CLI直接部署) | Git 自动部署 |
|------|----------------------|-------------|
| 部署方式 | 手动运行 `vercel --prod` | 推送到 GitHub 自动部署 |
| 版本控制 | ❌ 无 | ✅ Git 历史 |
| 回滚 | ❌ 困难 | ✅ 一键回滚 |
| 团队协作 | ❌ 困难 | ✅ 多人协作 |
| CI/CD | ❌ 无 | ✅ 自动化 |
| 推荐度 | ⚠️ 临时使用 | ✅ 生产环境 |

---

## 💡 **我的建议**

### 方案 1: 立即切换到 Git（推荐）
**优点**: 长期最佳实践，自动化部署
**时间**: 10-15分钟设置

### 方案 2: 暂时保持现状
**优点**: 已经部署完成，可以立即测试
**缺点**: 后续更新需要手动部署

---

## 🚀 **快速设置脚本**

如果你想立即切换到 Git 方式，运行：

```bash
# 设置 Git 并推送（需要先在 GitHub 创建仓库）
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

# Agent Portal
cd pivota-agents-portal && git init && git add -A && git commit -m "Initial commit" && git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git && git push -u origin main

# Merchant Portal  
cd ../pivota-merchants-portal && git init && git add -A && git commit -m "Initial commit" && git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git && git push -u origin main

# Employee Portal
cd ../pivota-employee-portal && git init && git add -A && git commit -m "Initial commit" && git remote add origin https://github.com/pengxu9-rgb/pivota-employee-portal.git && git push -u origin main
```

然后在 Vercel Dashboard 连接这些仓库。

---

## ✅ **当前可用状态**

**好消息：即使不连接 Git，你的网站现在也完全可用！**

- ✅ https://agents.pivota.cc - 可访问
- ✅ https://merchants.pivota.cc - 可访问
- ✅ https://employee.pivota.cc - 可访问
- ✅ 所有功能正常工作

你可以：
1. **先测试**：使用当前部署测试所有功能
2. **后续再连接 Git**：确认一切正常后再设置 Git 自动部署

**建议：先全面测试，确认没问题后再设置 Git Repository。** 

需要我帮你现在就设置 Git 吗？还是先测试一下系统？
