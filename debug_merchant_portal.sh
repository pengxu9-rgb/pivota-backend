#!/bin/bash

echo "🔍 调试 Merchant Portal 数据问题"
echo "=========================================="

API_URL="https://web-production-fedb.up.railway.app"

# 1. 登录获取 token
echo "步骤 1: 登录获取 token..."
LOGIN_RESPONSE=$(curl -sS -X POST "$API_URL/login" \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@pivota.cc", "password": "Admin123!"}')

TOKEN=$(echo $LOGIN_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
USER_INFO=$(echo $LOGIN_RESPONSE | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin)['user']))" 2>/dev/null)

echo "User info: $USER_INFO"
echo ""

# 2. 测试 merchant 端点
echo "步骤 2: 测试 /merchant/merch_208139f7600dbf42/integrations..."
curl -sS "$API_URL/merchant/merch_208139f7600dbf42/integrations" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo ""
echo "步骤 3: 测试 /merchant/merch_208139f7600dbf42/orders..."
curl -sS "$API_URL/merchant/merch_208139f7600dbf42/orders?limit=2" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30

echo ""
echo "步骤 4: 测试 /merchant/dashboard/stats..."
curl -sS "$API_URL/merchant/dashboard/stats" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30

echo ""
echo "步骤 5: 测试 /merchant/dashboard/analytics..."
curl -sS "$API_URL/merchant/dashboard/analytics" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30

