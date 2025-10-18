# 🔗 Vercel Git 连接配置

## 📊 当前状态

### lovable-website (宣传主页)
- ✅ 代码已推送到 GitHub: `https://github.com/pengxu9-rgb/pivota-ai-flow-26.git`
- ✅ HeroSection 更新已提交 (commit: 0211e4b)
- ⚠️ Vercel 可能未配置 GitHub 自动部署

### 三个新门户
- ✅ 已通过 Vercel CLI 直接部署
- ⏳ 建议连接到 GitHub（需要先创建仓库）

---

## 🔧 修复 lovable-website 的自动部署

### 在 Vercel Dashboard 检查 Git 连接

1. **访问 Vercel Dashboard**
   ```
   https://vercel.com/dashboard
   → 选择 "lovable-website" 项目
   ```

2. **检查 Git 连接状态**
   ```
   Settings → Git
   ```
   
   应该看到：
   - **Git Repository**: `pengxu9-rgb/pivota-ai-flow-26`
   - **Production Branch**: `main`
   - **Automatic Deployments**: ✅ Enabled

3. **如果没有连接 Git**
   ```
   Settings → Git → Connect Git Repository
   → 选择 GitHub
   → 授权 Vercel 访问 GitHub
   → 选择 pivota-ai-flow-26 仓库
   → 选择 main 分支作为 Production Branch
   → 保存
   ```

4. **手动触发重新部署**
   ```
   Deployments → 选择最新部署
   → 点击 "..." 菜单
   → Redeploy
   ```

---

## ✅ 验证 Git 连接成功

连接成功后，测试自动部署：

```bash
cd lovable-website

# 1. 做一个小改动（或空提交）
git commit --allow-empty -m "Test auto-deploy"

# 2. 推送到 GitHub
git push origin main

# 3. 观察 Vercel Dashboard
# 应该会自动创建新的部署
# 1-2 分钟后部署完成
```

---

## 📋 完整的 Git 设置流程

### 对于三个新门户项目

#### Step 1: 创建 GitHub 仓库

访问 https://github.com/new，创建三个仓库：
```
1. pivota-agents-portal (Public)
2. pivota-merchants-portal (Public)
3. pivota-employee-portal (Private 推荐)
```

#### Step 2: 推送代码到 GitHub

```bash
# Agent Portal
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-agents-portal
git init
git add -A
git commit -m "Initial commit: Agent Portal"
git branch -M main
git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git
git push -u origin main

# Merchant Portal
cd ../pivota-merchants-portal
git init
git add -A
git commit -m "Initial commit: Merchant Portal"
git branch -M main
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
git push -u origin main

# Employee Portal
cd ../pivota-employee-portal
git init
git add -A
git commit -m "Initial commit: Employee Portal"
git branch -M main
git remote add origin https://github.com/pengxu9-rgb/pivota-employee-portal.git
git push -u origin main
```

#### Step 3: 在 Vercel 连接 Git

**方式 A: 重新导入（推荐）**

1. 删除当前的三个项目（或保留作为备份）
2. 在 Vercel Dashboard 点击 "Add New Project"
3. 选择 "Import Git Repository"
4. 选择三个刚创建的仓库
5. 配置环境变量（从 vercel.json 复制）
6. 部署
7. 添加自定义域名

**方式 B: 修改现有项目设置**

1. 进入每个项目的 Settings
2. Git → Connect Git Repository
3. 选择对应的 GitHub 仓库
4. 保存

---

## 🎯 最终效果

配置完成后：

```
GitHub 推送 → 自动触发 Vercel 部署 → 1-2分钟后网站更新
```

**示例工作流程：**
```bash
# 1. 修改 Agent Portal 代码
cd pivota-agents-portal
# ... 编辑文件 ...

# 2. 提交并推送
git add .
git commit -m "Add new feature"
git push

# 3. Vercel 自动部署
# ✅ https://agents.pivota.cc 自动更新
```

---

## 💡 当前建议

### 选项 1: 先测试，后续再设置 Git（快速）
- 当前三个门户已部署可用
- 可以立即测试所有功能
- 确认没问题后再设置 Git

### 选项 2: 立即设置 Git（长期最佳）
- 在 GitHub 创建三个仓库
- 推送代码
- 在 Vercel 重新连接
- 删除旧的直接部署

---

## 🚀 你想怎么做？

1. **我帮你立即在 GitHub 创建仓库并推送**
2. **或者你先手动在 Vercel Dashboard 配置 lovable-website 的 Git 连接**
3. **或者先测试当前部署，确认功能正常后再设置**

请告诉我你的选择，我会协助完成！
