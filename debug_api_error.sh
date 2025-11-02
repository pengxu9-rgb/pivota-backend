#!/bin/bash

# Debug API error - show raw response
TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Testing /employee/agents endpoint"
echo "======================================"
echo ""

echo "Raw response (with headers):"
echo "--------------------------------------"
curl -i -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"

echo ""
echo ""
echo "======================================"
echo "🔍 Testing /employee/agents/{id}/details"
echo "======================================"
echo ""

echo "Raw response:"
echo "--------------------------------------"
curl -i -X GET "$API_URL/employee/agents/agent_ee38f2b3645a2ec2/details" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"

echo ""
echo ""
echo "======================================"
echo "💡 Check for:"
echo "======================================"
echo "- Status code (200, 404, 500?)"
echo "- Response body (JSON, HTML, empty?)"
echo "- Error messages"
echo "======================================"

