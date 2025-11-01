#!/bin/bash

# Script to test and fix agent metrics issue
# Usage: ./test_fix_agent_metrics.sh TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    echo "Please provide your admin token"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "1. Checking current metrics status..."
echo "======================================"
curl -s -X GET "$API_URL/admin/fix/agent-metrics-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "2. Fixing agent metrics..."
echo "======================================"
curl -s -X POST "$API_URL/admin/fix/agent-metrics" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "3. Checking metrics after fix..."
echo "======================================"
curl -s -X GET "$API_URL/admin/fix/agent-metrics-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "4. Fetching updated agent data..."
echo "======================================"
curl -s -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool | head -50

echo ""
echo "======================================"
echo "Fix complete! Refresh the Employee Portal to see updated metrics."
echo "======================================" 
