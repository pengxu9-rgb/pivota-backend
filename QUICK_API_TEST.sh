#!/bin/bash
# Quick API Test - Agent SDK Ready Endpoints

BASE="https://web-production-fedb.up.railway.app/agent/v1"

echo "======================================================"
echo "  🚀 Pivota Agent API - Quick Test"
echo "======================================================"

# Test Health
echo -e "\n✅ 1. Health Check"
curl -s "$BASE/health" | python3 -m json.tool

# Generate API Key
echo -e "\n✅ 2. Generate API Key"
RESPONSE=$(curl -s -X POST "$BASE/auth" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "Quick Test Agent",
    "agent_email": "quicktest@pivota.com"
  }')

echo "$RESPONSE" | python3 -m json.tool
API_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null)

if [ ! -z "$API_KEY" ]; then
    echo -e "\n✅ Got API Key: ${API_KEY:0:30}..."
    
    # Test Merchants
    echo -e "\n✅ 3. List Merchants"
    curl -s "$BASE/merchants?limit=3" \
      -H "X-API-Key: $API_KEY" | python3 -m json.tool
    
    # Test Rate Limits
    echo -e "\n✅ 4. Check Rate Limits"
    curl -s "$BASE/rate-limits" \
      -H "X-API-Key: $API_KEY" | python3 -m json.tool
fi

echo -e "\n======================================================"
echo "  ✅ Quick Test Complete"
echo "======================================================"
