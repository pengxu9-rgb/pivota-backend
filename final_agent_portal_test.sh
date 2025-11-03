#!/bin/bash

API="https://web-production-fedb.up.railway.app"
AGENT_EMAIL="asdf@asdf.com"
AGENT_PASSWORD="Qwer1234"

echo "========================================="
echo "Agent Portal 完整测试"
echo "========================================="
echo ""

# 1. 登录
echo "1️⃣ 登录测试..."
LOGIN=$(curl -s -X POST "$API/agent/account/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$AGENT_EMAIL\", \"password\": \"$AGENT_PASSWORD\"}")

if echo "$LOGIN" | grep -q '"success": true'; then
  TOKEN=$(echo "$LOGIN" | python3 -c "import sys, json; print(json.load(sys.stdin)['token'])" 2>/dev/null)
  AGENT_ID=$(echo "$LOGIN" | python3 -c "import sys, json; print(json.load(sys.stdin)['agent']['agent_id'])" 2>/dev/null)
  
  echo "✅ 登录成功"
  echo "   Agent ID: $AGENT_ID"
  echo "   Token: ${TOKEN:0:50}..."
  
  # 2-6. 测试所有 API
  echo ""
  echo "2️⃣ Merchants API..."
  MERCHANTS=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X GET "$API/agents/$AGENT_ID/merchants" \
    -H "Authorization: Bearer $TOKEN")
  HTTP_CODE=$(echo "$MERCHANTS" | grep "HTTP_CODE:" | cut -d: -f2)
  RESPONSE=$(echo "$MERCHANTS" | sed '/HTTP_CODE:/d')
  
  if [ "$HTTP_CODE" = "200" ]; then
    COUNT=$(echo "$RESPONSE" | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('merchants', [])))" 2>/dev/null)
    echo "   ✅ 成功 (HTTP $HTTP_CODE) - $COUNT 个商户"
  else
    echo "   ❌ 失败 (HTTP $HTTP_CODE)"
    echo "$RESPONSE" | python3 -m json.tool 2>/dev/null | head -3
  fi
  
  echo ""
  echo "3️⃣ Orders API..."
  ORDERS=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X GET "$API/agent/v1/orders?limit=10" \
    -H "Authorization: Bearer $TOKEN")
  HTTP_CODE=$(echo "$ORDERS" | grep "HTTP_CODE:" | cut -d: -f2)
  
  if [ "$HTTP_CODE" = "200" ]; then
    COUNT=$(echo "$ORDERS" | sed '/HTTP_CODE:/d' | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('orders', [])))" 2>/dev/null)
    echo "   ✅ 成功 (HTTP $HTTP_CODE) - $COUNT 个订单"
  else
    echo "   ❌ 失败 (HTTP $HTTP_CODE)"
  fi
  
  echo ""
  echo "4️⃣ Revenue Expectations API..."
  REV=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X GET "$API/agents/$AGENT_ID/revenue/expectations" \
    -H "Authorization: Bearer $TOKEN")
  HTTP_CODE=$(echo "$REV" | grep "HTTP_CODE:" | cut -d: -f2)
  
  if [ "$HTTP_CODE" = "200" ]; then
    echo "   ✅ 成功 (HTTP $HTTP_CODE)"
  else
    echo "   ❌ 失败 (HTTP $HTTP_CODE)"
  fi
  
  echo ""
  echo "5️⃣ Settlements API..."
  SETTLEMENTS=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X GET "$API/agents/$AGENT_ID/settlements" \
    -H "Authorization: Bearer $TOKEN")
  HTTP_CODE=$(echo "$SETTLEMENTS" | grep "HTTP_CODE:" | cut -d: -f2)
  
  if [ "$HTTP_CODE" = "200" ]; then
    echo "   ✅ 成功 (HTTP $HTTP_CODE)"
  else
    echo "   ❌ 失败 (HTTP $HTTP_CODE)"
  fi
  
  echo ""
  echo "6️⃣ Metrics Summary API..."
  METRICS=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X GET "$API/agent/metrics/summary" \
    -H "Authorization: Bearer $TOKEN")
  HTTP_CODE=$(echo "$METRICS" | grep "HTTP_CODE:" | cut -d: -f2)
  
  if [ "$HTTP_CODE" = "200" ]; then
    echo "   ✅ 成功 (HTTP $HTTP_CODE)"
  else
    echo "   ❌ 失败 (HTTP $HTTP_CODE)"
  fi
  
  echo ""
  echo "========================================="
  echo "📊 测试总结"
  echo "========================================="
  echo ""
  echo "如果所有 API 都是 ✅，Agent Portal 应该能正常工作！"
  echo ""
  echo "🌐 访问: https://agents.pivota.cc/"
  echo "📧 Email: $AGENT_EMAIL"  
  echo "🔑 Password: $AGENT_PASSWORD"
  echo ""
  echo "⚠️ 记得清除浏览器缓存/localStorage 后重新登录"
  
else
  echo "❌ 登录失败！"
  echo "$LOGIN" | python3 -m json.tool 2>/dev/null
fi

echo ""
echo "========================================="
