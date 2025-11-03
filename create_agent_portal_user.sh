#!/bin/bash

# Agent Portal 用户创建脚本
# 为现有的 agent_ee38f2b3645a2ec2 创建登录账户

API="https://web-production-fedb.up.railway.app"
EMPLOYEE_TOKEN="$1"

if [ -z "$EMPLOYEE_TOKEN" ]; then
  echo "用法: ./create_agent_portal_user.sh <EMPLOYEE_TOKEN>"
  echo ""
  echo "获取 Employee Token:"
  echo "1. 访问 https://employee.pivota.cc/"
  echo "2. 登录 (employee@pivota.com)"
  echo "3. 打开浏览器控制台"
  echo "4. 运行: localStorage.getItem('employee_token')"
  echo "5. 复制 token 并作为参数传入"
  exit 1
fi

echo "========================================="
echo "Agent Portal 用户创建脚本"
echo "========================================="
echo ""

AGENT_ID="agent_ee38f2b3645a2ec2"
AGENT_EMAIL="agent@pivota.com"
AGENT_PASSWORD="Agent123456"

echo "📋 目标 Agent: $AGENT_ID"
echo "📧 Email: $AGENT_EMAIL"
echo "🔑 Password: $AGENT_PASSWORD"
echo ""

# 1. 检查 agent 是否已存在
echo "1️⃣ 检查 agent 是否存在..."
AGENT_INFO=$(curl -s -X GET "$API/employee/agents/$AGENT_ID/details" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN")

echo "$AGENT_INFO" | python3 -m json.tool 2>/dev/null | head -20

# 2. 检查 users 表中是否有对应账户
echo ""
echo "2️⃣ 检查是否已有 agent 用户账户..."

CHECK_USER=$(curl -s -X POST "$API/admin/sql-execute" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"SELECT id, email, role, active FROM users WHERE email = '$AGENT_EMAIL' LIMIT 1\"
  }")

echo "$CHECK_USER" | python3 -m json.tool 2>/dev/null

# 3. 如果不存在，创建 agent 用户
if echo "$CHECK_USER" | grep -q '"rows": \[\]'; then
  echo ""
  echo "3️⃣ 用户不存在，正在创建..."
  
  CREATE_USER=$(curl -s -X POST "$API/admin/sql-execute" \
    -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
      \"query\": \"INSERT INTO users (email, password_hash, full_name, role, active, created_at) VALUES ('$AGENT_EMAIL', '\$2b\$12\$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5ViT0VrKZOBBu', 'Pivota Agent', 'agent', true, NOW()) RETURNING id, email, role\"
    }")
  
  echo "$CREATE_USER" | python3 -m json.tool 2>/dev/null
  
  if echo "$CREATE_USER" | grep -q '"status": "success"'; then
    echo "✅ Agent 用户创建成功！"
  else
    echo "❌ 创建失败，可能已存在"
  fi
else
  echo "✅ Agent 用户已存在"
fi

echo ""
echo "========================================="
echo "✅ Agent Portal 登录凭据"
echo "========================================="
echo ""
echo "🌐 登录地址: https://agents.pivota.cc/login"
echo "📧 Email: $AGENT_EMAIL"
echo "🔑 Password: $AGENT_PASSWORD"
echo ""
echo "========================================="
echo "📝 测试步骤:"
echo "========================================="
echo ""
echo "1. 清除浏览器缓存和 localStorage:"
echo "   - 打开开发者工具 (F12)"
echo "   - Application → Storage → Clear site data"
echo ""
echo "2. 访问登录页面并使用上述凭据登录"
echo ""
echo "3. 登录成功后，检查 localStorage:"
echo "   localStorage.getItem('agent_token')  // 应该是真实的 JWT"
echo "   localStorage.getItem('agent_id')     // 应该是 $AGENT_ID"
echo ""
echo "4. 访问 /merchants 和 /revenue 页面验证数据"
echo ""
echo "========================================="

# 4. 测试登录 API
echo ""
echo "4️⃣ 测试登录 API..."
LOGIN_TEST=$(curl -s -X POST "$API/agent/account/login" \
  -H "Content-Type: application/json" \
  -d "{
    \"email\": \"$AGENT_EMAIL\",
    \"password\": \"$AGENT_PASSWORD\"
  }")

echo "$LOGIN_TEST" | python3 -m json.tool 2>/dev/null | head -30

if echo "$LOGIN_TEST" | grep -q '"success": true'; then
  echo ""
  echo "✅ 登录测试成功！Token 已生成"
  echo ""
  echo "🎉 Agent Portal 现在可以使用真实认证了！"
else
  echo ""
  echo "⚠️ 登录测试失败，请检查:"
  echo "   - users 表是否有 $AGENT_EMAIL 且 role='agent'"
  echo "   - password_hash 是否正确"
  echo "   - agents 表是否有对应记录"
fi

echo ""
echo "========================================="
echo "脚本完成！"
echo "========================================="

