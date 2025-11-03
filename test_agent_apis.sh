#!/bin/bash

API="https://web-production-fedb.up.railway.app"
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhc2RmQGFzZGYuY29tIiwidXNlcl9pZCI6IjI5NjFjNTM3LWNkZmItNGI3NC04NTZiLTMwNDdlN2EzNjkwZSIsInJvbGUiOiJhZ2VudCIsImFnZW50X2lkIjoiYWdlbnRfZWUzOGYyYjM2NDVhMmVjMiIsImV4cCI6MTc2MjI0NjE5MiwiaWF0IjoxNzYyMTU5NzkyfQ.52ZSQqec2dskueZ3cm3WA4qIsIFYuMCVvtoAtDL5IbY"
AGENT_ID="agent_ee38f2b3645a2ec2"

echo "========================================="
echo "Agent Portal API 测试"
echo "========================================="
echo ""
echo "Agent ID: $AGENT_ID"
echo ""

# Test Merchants
echo "1️⃣ 测试 Merchants API..."
curl -s -X GET "$API/agents/$AGENT_ID/merchants" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool 2>/dev/null | head -30

echo ""
echo "========================================="

# Test Orders
echo "2️⃣ 测试 Orders API..."
curl -s -X GET "$API/agent/v1/orders?limit=5" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool 2>/dev/null | head -30

echo ""
echo "========================================="

# Test Revenue
echo "3️⃣ 测试 Revenue Expectations..."
curl -s -X GET "$API/agents/$AGENT_ID/revenue/expectations" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool 2>/dev/null

echo ""
echo "========================================="

# Test Settlements
echo "4️⃣ 测试 Settlements..."
curl -s -X GET "$API/agents/$AGENT_ID/settlements" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool 2>/dev/null | head -20

echo ""
echo "========================================="

# Test Metrics
echo "5️⃣ 测试 Metrics Summary..."
curl -s -X GET "$API/agent/metrics/summary" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool 2>/dev/null | head -40

echo ""
echo "========================================="
echo "✅ 测试完成"
echo "========================================="
