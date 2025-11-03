#!/bin/bash

API="https://web-production-fedb.up.railway.app"
AGENT_EMAIL="asdf@asdf.com"
AGENT_PASSWORD="Qwer1234"

echo "========================================="
echo "🧪 Agent Portal 最终测试"
echo "========================================="
echo ""

# 1. 登录并获取新 token（带 email 字段）
echo "1️⃣ 登录获取新 token..."
LOGIN=$(curl -s -X POST "$API/agent/account/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$AGENT_EMAIL\", \"password\": \"$AGENT_PASSWORD\"}")

if echo "$LOGIN" | grep -q '"success": true'; then
  TOKEN=$(echo "$LOGIN" | python3 -c "import sys, json; print(json.load(sys.stdin)['token'])")
  AGENT_ID=$(echo "$LOGIN" | python3 -c "import sys, json; print(json.load(sys.stdin)['agent']['agent_id'])")
  
  echo "✅ 登录成功"
  echo "   Agent ID: $AGENT_ID"
  
  # 解码 token 查看 payload
  echo ""
  echo "🔍 Token Payload:"
  PAYLOAD=$(echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null)
  echo "$PAYLOAD" | python3 -m json.tool 2>/dev/null | grep -E '"email"|"agent_id"|"role"'
  
  # 测试所有 API
  declare -a TESTS=(
    "Merchants:GET:/agents/$AGENT_ID/merchants"
    "Orders:GET:/agent/v1/orders?limit=5"
    "Revenue Expectations:GET:/agents/$AGENT_ID/revenue/expectations"
    "Settlements:GET:/agents/$AGENT_ID/settlements"
    "Metrics Summary:GET:/agent/metrics/summary"
  )
  
  echo ""
  echo "========================================="
  echo "📊 测试所有 API 端点"
  echo "========================================="
  echo ""
  
  SUCCESS=0
  FAIL=0
  
  for test in "${TESTS[@]}"; do
    NAME=$(echo "$test" | cut -d: -f1)
    METHOD=$(echo "$test" | cut -d: -f2)
    ENDPOINT=$(echo "$test" | cut -d: -f3-)
    
    RESPONSE=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X $METHOD "$API$ENDPOINT" \
      -H "Authorization: Bearer $TOKEN")
    
    HTTP_CODE=$(echo "$RESPONSE" | grep "HTTP_CODE:" | cut -d: -f2)
    
    if [ "$HTTP_CODE" = "200" ]; then
      echo "✅ $NAME (HTTP $HTTP_CODE)"
      ((SUCCESS++))
    else
      echo "❌ $NAME (HTTP $HTTP_CODE)"
      ((FAIL++))
      echo "$RESPONSE" | sed '/HTTP_CODE:/d' | python3 -m json.tool 2>/dev/null | head -5 | sed 's/^/   /'
    fi
  done
  
  echo ""
  echo "========================================="
  echo "📈 测试结果"
  echo "========================================="
  echo ""
  echo "✅ 成功: $SUCCESS"
  echo "❌ 失败: $FAIL"
  echo ""
  
  if [ $FAIL -eq 0 ]; then
    echo "🎉 所有 API 都正常工作！"
    echo ""
    echo "现在在浏览器中:"
    echo "1. 访问 https://agents.pivota.cc/"
    echo "2. 清除 localStorage (F12 → Application → Clear site data)"
    echo "3. 登录: $AGENT_EMAIL / $AGENT_PASSWORD"
    echo "4. 访问 Merchants/Orders/Revenue 页面验证数据"
  else
    echo "⚠️ 仍有 $FAIL 个 API 失败，需要进一步调试"
  fi
  
else
  echo "❌ 登录失败！"
  echo "$LOGIN" | python3 -m json.tool 2>/dev/null
fi

echo ""
echo "========================================="
