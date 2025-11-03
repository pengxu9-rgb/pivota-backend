#!/bin/bash

# 快速测试 Agent Portal 登录

API="https://web-production-fedb.up.railway.app"
AGENT_EMAIL="agent@pivota.com"
AGENT_PASSWORD="Agent123456"

echo "========================================="
echo "Agent Portal 登录测试"
echo "========================================="
echo ""
echo "📧 Email: $AGENT_EMAIL"
echo "🔑 Password: $AGENT_PASSWORD"
echo ""
echo "测试登录 API..."
echo ""

LOGIN_RESPONSE=$(curl -s -X POST "$API/agent/account/login" \
  -H "Content-Type: application/json" \
  -d "{
    \"email\": \"$AGENT_EMAIL\",
    \"password\": \"$AGENT_PASSWORD\"
  }")

echo "$LOGIN_RESPONSE" | python3 -m json.tool 2>/dev/null

if echo "$LOGIN_RESPONSE" | grep -q '"success": true'; then
  echo ""
  echo "✅ 登录成功！"
  
  TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['token'])" 2>/dev/null)
  AGENT_ID=$(echo "$LOGIN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['agent']['agent_id'])" 2>/dev/null)
  
  echo ""
  echo "🔑 Token: ${TOKEN:0:50}..."
  echo "👤 Agent ID: $AGENT_ID"
  echo ""
  echo "测试 Merchants API..."
  
  MERCHANTS=$(curl -s -X GET "$API/agents/$AGENT_ID/merchants" \
    -H "Authorization: Bearer $TOKEN")
  
  echo "$MERCHANTS" | python3 -m json.tool 2>/dev/null | head -30
  
  if echo "$MERCHANTS" | grep -q '"merchants"'; then
    echo ""
    echo "✅ Merchants API 工作正常！"
  else
    echo ""
    echo "⚠️ Merchants API 返回错误"
  fi
else
  echo ""
  echo "❌ 登录失败！"
  echo ""
  echo "需要先创建 agent 用户账户。"
  echo "请运行: ./create_agent_portal_user.sh <EMPLOYEE_TOKEN>"
fi

echo ""
echo "========================================="
