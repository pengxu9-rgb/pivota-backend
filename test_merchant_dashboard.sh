#!/bin/bash

echo "🧪 测试 Merchant Dashboard API"
echo "=========================================="

API_URL="https://web-production-fedb.up.railway.app"

# 步骤 1: 登录获取 token
echo "步骤 1: 登录..."
LOGIN_RESPONSE=$(curl -sS -X POST "$API_URL/signin" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@pivota.cc",
    "password": "Admin123!"
  }')

echo "$LOGIN_RESPONSE" | python3 -m json.tool

TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('token', ''))" 2>/dev/null)

if [ -z "$TOKEN" ]; then
  echo "❌ 登录失败，无法获取 token"
  exit 1
fi

echo ""
echo "✅ Token 获取成功"
echo ""

# 步骤 2: 测试 Dashboard Stats API
echo "步骤 2: 测试 Dashboard Stats API..."
curl -sS "$API_URL/merchant/dashboard/stats" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo ""
echo ""

# 步骤 3: 测试 User Info API
echo "步骤 3: 测试 User Info API..."
curl -sS "$API_URL/user" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
