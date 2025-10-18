#!/bin/bash

# 🚀 部署三个门户到 Vercel
# 运行前请确保：
# 1. 已在 GitHub 创建三个仓库
# 2. 已安装 Vercel CLI: npm i -g vercel

echo "🚀 Pivota 三个门户自动化部署脚本"
echo "======================================"
echo ""

BASE_DIR="/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"

# ========================================
# 1. Agent Portal
# ========================================
echo "1️⃣ 部署 Agent Portal (agents.pivota.cc)..."
cd "$BASE_DIR/pivota-agents-portal"

# 初始化 Git
git init
git add -A
git commit -m "Initial commit: Agent Portal with MCP/API Integration"

# 推送到 GitHub (请先在 GitHub 创建仓库: pivota-agents-portal)
echo "📝 请在 GitHub 创建仓库: pivota-agents-portal"
echo "然后运行: git remote add origin https://github.com/pengxu9-rgb/pivota-agents-portal.git"
echo "git branch -M main && git push -u origin main"
read -p "按Enter继续..."

# Vercel 部署
vercel --prod
vercel domains add agents.pivota.cc

echo "✅ Agent Portal 部署完成"
echo ""

# ========================================
# 2. Merchant Portal
# ========================================
echo "2️⃣ 部署 Merchant Portal (merchants.pivota.cc)..."
cd "$BASE_DIR/pivota-merchants-portal"

git init
git add -A
git commit -m "Initial commit: Merchant Portal with Onboarding Flow"

echo "📝 请在 GitHub 创建仓库: pivota-merchants-portal"
read -p "按Enter继续..."

vercel --prod
vercel domains add merchants.pivota.cc

echo "✅ Merchant Portal 部署完成"
echo ""

# ========================================
# 3. Employee Portal
# ========================================
echo "3️⃣ 部署 Employee Portal (employee.pivota.cc)..."
cd "$BASE_DIR/pivota-employee-portal"

git init
git add -A
git commit -m "Initial commit: Employee Portal for Internal Management"

echo "📝 请在 GitHub 创建仓库: pivota-employee-portal"
read -p "按 Enter继续..."

vercel --prod
vercel domains add employee.pivota.cc

echo "✅ Employee Portal 部署完成"
echo ""

# ========================================
# 完成
# ========================================
echo "🎉 所有三个门户部署完成！"
echo ""
echo "请访问以下链接验证："
echo "✅ https://agents.pivota.cc"
echo "✅ https://merchants.pivota.cc"
echo "✅ https://employee.pivota.cc"
echo ""
echo "🌐 主页链接也已更新："
echo "✅ https://pivota.cc (点击两个入口按钮)"
