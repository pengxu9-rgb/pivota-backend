# 🚀 推送到 GitHub - 快速指南

## ✅ Git 已初始化

三个项目的 Git 仓库已初始化并创建了初始提交：
- ✅ pivota-agents-portal (948d37a)
- ✅ pivota-merchants-portal (5a618f5)
- ✅ pivota-employee-portal (bf16449)

---

## 📝 Step 1: 在 GitHub 创建三个仓库

访问：https://github.com/new

创建以下仓库：

### 1. pivota-agents-portal
- Repository name: `pivota-agents-portal`
- Description: `Agent Portal - MCP/API Integration for AI Agents`
- Visibility: **Public** (推荐，便于推广)
- ❌ 不要勾选 "Initialize this repository with a README"

### 2. pivota-merchants-portal
- Repository name: `pivota-merchants-portal`
- Description: `Merchant Portal - Onboarding and Store Management`
- Visibility: **Public**
- ❌ 不要勾选 "Initialize this repository with a README"

### 3. pivota-employee-portal
- Repository name: `pivota-employee-portal`
- Description: `Employee Portal - Internal Management System`
- Visibility: **Private** (推荐，内部使用)
- ❌ 不要勾选 "Initialize this repository with a README"

---

## 🔗 Step 2: 推送代码到 GitHub

创建好仓库后，运行以下命令：

```bash
# 1. Agent Portal
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-agents-portal
git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git
git branch -M main
git push -u origin main

# 2. Merchant Portal
cd ../pivota-merchants-portal
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
git branch -M main
git push -u origin main

# 3. Employee Portal
cd ../pivota-employee-portal
git remote add origin https://github.com/pengxu9-rgb/pivota-employee-portal.git
git branch -M main
git push -u origin main
```

---

## 🔄 Step 3: 在 Vercel 连接 GitHub

推送成功后，在 Vercel Dashboard：

### 方式 A: 保持现有项目，添加 Git 连接

对于每个项目（pivota-agents-portal, pivota-merchants-portal, pivota-employee-portal）：

1. 进入项目 Settings
2. 点击 "Git" 标签
3. 点击 "Connect Git Repository"
4. 选择对应的 GitHub 仓库
5. Production Branch: `main`
6. 保存

### 方式 B: 删除并重新导入（更干净）

1. 删除现有的三个 Vercel 项目
2. 点击 "Add New Project"
3. "Import Git Repository"
4. 选择三个 GitHub 仓库
5. 配置环境变量（Vercel 会自动读取 vercel.json）
6. 部署
7. 添加自定义域名（agents/merchants/employee.pivota.cc）

---

## ⚡ 一键执行脚本

创建好 GitHub 仓库后，运行：

```bash
bash << 'EOF'
BASE="/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"

echo "🚀 开始推送到 GitHub..."
echo ""

# Agent Portal
echo "1️⃣ Pushing Agent Portal..."
cd "$BASE/pivota-agents-portal"
git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git 2>/dev/null || true
git push -u origin main
echo "✅ Agent Portal 推送完成"
echo ""

# Merchant Portal
echo "2️⃣ Pushing Merchant Portal..."
cd "$BASE/pivota-merchants-portal"
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git 2>/dev/null || true
git push -u origin main
echo "✅ Merchant Portal 推送完成"
echo ""

# Employee Portal
echo "3️⃣ Pushing Employee Portal..."
cd "$BASE/pivota-employee-portal"
git remote add origin https://github.com/pengxu9-rgb/pivota-employee-portal.git 2>/dev/null || true
git push -u origin main
echo "✅ Employee Portal 推送完成"
echo ""

echo "🎉 所有项目已推送到 GitHub！"
echo ""
echo "📋 下一步："
echo "1. 访问 https://vercel.com/dashboard"
echo "2. 为三个项目连接 Git Repository"
echo "3. 或删除现有项目，重新从 GitHub 导入"
EOF
```

---

## 📊 验证 Git 推送成功

推送后访问：
- https://github.com/pengxu9-rgb/pivota-agents-portal
- https://github.com/pengxu9-rgb/pivota-merchants-portal
- https://github.com/pengxu9-rgb/pivota-employee-portal

应该能看到完整的代码。

---

**准备好了吗？请先在 GitHub 创建三个仓库，然后告诉我，我会立即帮你推送代码！** 🚀
