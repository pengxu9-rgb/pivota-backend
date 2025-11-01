#!/bin/bash

# Script to check full agent data structure
# Usage: ./check_full_agent_data.sh TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    echo "Please provide your admin token"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "1. Checking agent list from Employee API..."
echo "======================================"
curl -s -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "2. Getting first agent details..."
echo "======================================"
# Extract first agent ID
AGENT_ID=$(curl -s -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['agents'][0]['agent_id'] if data['agents'] else '')")

if [ -n "$AGENT_ID" ]; then
    echo "Fetching details for agent: $AGENT_ID"
    curl -s -X GET "$API_URL/employee/agents/$AGENT_ID/details" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" | python3 -m json.tool
else
    echo "No agent found"
fi

echo ""
echo "======================================"
echo "Compare the field names above with what frontend expects"
echo "======================================"
