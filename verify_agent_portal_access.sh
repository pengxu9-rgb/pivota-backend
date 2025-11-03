#!/bin/bash

# Agent Portal API 访问验证脚本
# 使用你的登录凭据: asdf@asdf.com / Qwer1234

API="https://web-production-fedb.up.railway.app"
AGENT_EMAIL="asdf@asdf.com"
AGENT_PASSWORD="Qwer1234"

echo "========================================="
echo "Agent Portal API 访问验证"
echo "========================================="
echo ""

# 1. 登录并获取 token
echo "1️⃣ 登录 Agent Portal..."
LOGIN=$(curl -s -X POST "$API/agent/account/login" \
  -H "Content-Type: application/json" \
  -d "{
    \"email\": \"$AGENT_EMAIL\",
    \"password\": \"$AGENT_PASSWORD\"
  }")

echo "$LOGIN" | python3 -m json.tool 2>/dev/null | head -20

if echo "$LOGIN" | grep -q '"success": true'; then
  echo ""
  echo "✅ 登录成功！"
  
  TOKEN=$(echo "$LOGIN" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['token'])" 2>/dev/null)
  AGENT_ID=$(echo "$LOGIN" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['agent']['agent_id'])" 2>/dev/null)
  
  echo "🔑 Token: ${TOKEN:0:60}..."
  echo "👤 Agent ID: $AGENT_ID"
  
  # 2. 测试 Merchants API
  echo ""
  echo "========================================="
  echo "2️⃣ 测试 Merchants API"
  echo "========================================="
  MERCHANTS=$(curl -s -X GET "$API/agents/$AGENT_ID/merchants" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json")
  
  echo "$MERCHANTS" | python3 -m json.tool 2>/dev/null
  
  if echo "$MERCHANTS" | grep -q '"merchants"'; then
    MERCHANT_COUNT=$(echo "$MERCHANTS" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('merchants', [])))" 2>/dev/null)
    echo ""
    echo "✅ Merchants API 成功！找到 $MERCHANT_COUNT 个商户"
  else
    echo ""
    echo "❌ Merchants API 失败"
  fi
  
  # 3. 测试 Orders API
  echo ""
  echo "========================================="
  echo "3️⃣ 测试 Orders API"
  echo "========================================="
  ORDERS=$(curl -s -X GET "$API/agent/v1/orders?limit=10" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json")
  
  echo "$ORDERS" | python3 -m json.tool 2>/dev/null | head -30
  
  if echo "$ORDERS" | grep -q '"orders"'; then
    ORDER_COUNT=$(echo "$ORDERS" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('orders', [])))" 2>/dev/null)
    echo ""
    echo "✅ Orders API 成功！找到 $ORDER_COUNT 个订单"
  else
    echo ""
    echo "❌ Orders API 失败"
  fi
  
  # 4. 测试 Revenue Expectations API
  echo ""
  echo "========================================="
  echo "4️⃣ 测试 Revenue Expectations API"
  echo "========================================="
  EXPECTATIONS=$(curl -s -X GET "$API/agents/$AGENT_ID/revenue/expectations" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json")
  
  echo "$EXPECTATIONS" | python3 -m json.tool 2>/dev/null
  
  if echo "$EXPECTATIONS" | grep -q -E '"has_expectations|expected_commission_rate"'; then
    echo ""
    echo "✅ Revenue Expectations API 成功！"
  else
    echo ""
    echo "ℹ️ Revenue Expectations API 返回（可能还没有设置）"
  fi
  
  # 5. 测试 Settlements API
  echo ""
  echo "========================================="
  echo "5️⃣ 测试 Settlements API"
  echo "========================================="
  SETTLEMENTS=$(curl -s -X GET "$API/agents/$AGENT_ID/settlements" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json")
  
  echo "$SETTLEMENTS" | python3 -m json.tool 2>/dev/null | head -20
  
  if echo "$SETTLEMENTS" | grep -q '"settlements"'; then
    SETTLEMENT_COUNT=$(echo "$SETTLEMENTS" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('settlements', [])))" 2>/dev/null)
    echo ""
    echo "✅ Settlements API 成功！找到 $SETTLEMENT_COUNT 条记录"
  else
    echo ""
    echo "ℹ️ Settlements API 返回（可能还没有结算记录）"
  fi
  
  # 6. 测试 Metrics API
  echo ""
  echo "========================================="
  echo "6️⃣ 测试 Metrics API"
  echo "========================================="
  METRICS=$(curl -s -X GET "$API/agent/metrics/summary" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json")
  
  echo "$METRICS" | python3 -m json.tool 2>/dev/null | head -30
  
  if echo "$METRICS" | grep -q '"overview"'; then
    echo ""
    echo "✅ Metrics API 成功！"
  else
    echo ""
    echo "❌ Metrics API 失败"
  fi
  
  echo ""
  echo "========================================="
  echo "✅ 验证完成"
  echo "========================================="
  echo ""
  echo "如果所有 API 都返回 ✅，Agent Portal 应该能正常显示数据！"
  echo ""
  echo "🌐 访问: https://agents.pivota.cc/"
  echo "📧 Email: $AGENT_EMAIL"
  echo "🔑 Password: $AGENT_PASSWORD"
  
else
  echo ""
  echo "❌ 登录失败！"
  echo ""
  echo "请检查:"
  echo "1. Railway 后端是否部署成功"
  echo "2. 账户凭据是否正确"
  echo "3. users 表中是否有对应记录"
fi

echo ""
echo "========================================="

