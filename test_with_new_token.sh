#!/bin/bash
API="https://web-production-fedb.up.railway.app"
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhc2RmQGFzZGYuY29tIiwiZW1haWwiOiJhc2RmQGFzZGYuY29tIiwidXNlcl9pZCI6IjI5NjFjNTM3LWNkZmItNGI3NC04NTZiLTMwNDdlN2EzNjkwZSIsInJvbGUiOiJhZ2VudCIsImFnZW50X2lkIjoiYWdlbnRfZWUzOGYyYjM2NDVhMmVjMiIsImV4cCI6MTc2MjI0NjY3MywiaWF0IjoxNzYyMTYwMjczfQ.Udn7jd185xochRm_A0-l5enPoTW-7sOZ82w_3xAfibU"
AGENT_ID="agent_ee38f2b3645a2ec2"

echo "使用新 token 测试..."
echo "Token payload 解码:"
echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | python3 -m json.tool
echo ""

echo "========================================="
echo "测试 Merchants API"
echo "========================================="
curl -s -X GET "$API/agents/$AGENT_ID/merchants" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30

echo ""
echo "========================================="
echo "测试 Revenue Expectations API"
echo "========================================="
curl -s -X GET "$API/agents/$AGENT_ID/revenue/expectations" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo ""
echo "========================================="
