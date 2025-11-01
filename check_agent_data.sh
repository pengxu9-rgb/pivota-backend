#!/bin/bash

# Script to check actual agent data in the database
# Usage: ./check_agent_data.sh TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    echo "Please provide your admin token"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "Checking actual agent data..."
echo "======================================"

# Get all agents data via Employee API
echo "Fetching all agents via Employee API..."
curl -s -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "If you see agents with actual names above but the frontend"
echo "still shows 'Unnamed Agent', the issue is in the frontend"
echo "fallback logic, not the database."
echo "======================================"
