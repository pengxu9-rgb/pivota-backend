#!/bin/bash
# Agent API Testing Script
# Tests all SDK-ready endpoints

BASE_URL="https://web-production-fedb.up.railway.app"
AGENT_PREFIX="/agent/v1"

echo "======================================================"
echo "     🧪 Agent API Testing Suite"
echo "======================================================"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Health Check
echo -e "\n${YELLOW}Test 1: Health Check${NC}"
echo "GET $BASE_URL$AGENT_PREFIX/health"
HEALTH=$(curl -s "$BASE_URL$AGENT_PREFIX/health")
if echo "$HEALTH" | grep -q '"status":"ok"'; then
    echo -e "${GREEN}✅ Health check passed${NC}"
    echo "$HEALTH" | python3 -m json.tool | head -10
else
    echo -e "${RED}❌ Health check failed${NC}"
    echo "$HEALTH"
fi

# Test 2: Generate API Key
echo -e "\n${YELLOW}Test 2: Generate API Key${NC}"
echo "POST $BASE_URL$AGENT_PREFIX/auth"
AUTH_RESPONSE=$(curl -s -X POST "$BASE_URL$AGENT_PREFIX/auth" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "Test Agent",
    "agent_email": "test-agent@example.com",
    "description": "Test agent for API validation"
  }')

if echo "$AUTH_RESPONSE" | grep -q '"api_key"'; then
    echo -e "${GREEN}✅ API key generation passed${NC}"
    API_KEY=$(echo "$AUTH_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))")
    echo "   API Key: ${API_KEY:0:20}..."
else
    echo -e "${RED}❌ API key generation failed${NC}"
    echo "$AUTH_RESPONSE"
    API_KEY="test_key"
fi

# Test 3: List Merchants
echo -e "\n${YELLOW}Test 3: List Merchants${NC}"
echo "GET $BASE_URL$AGENT_PREFIX/merchants"
MERCHANTS=$(curl -s "$BASE_URL$AGENT_PREFIX/merchants?limit=5" \
  -H "X-API-Key: $API_KEY")

if echo "$MERCHANTS" | grep -q '"merchants"'; then
    echo -e "${GREEN}✅ Merchants list passed${NC}"
    echo "$MERCHANTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'   Found {len(d.get(\"merchants\",[]))} merchants')"
else
    echo -e "${RED}❌ Merchants list failed${NC}"
    echo "$MERCHANTS"
fi

# Test 4: Search Products
echo -e "\n${YELLOW}Test 4: Search Products${NC}"
echo "GET $BASE_URL$AGENT_PREFIX/products/search"
# Note: This requires valid merchant_id and agent context
echo "   (Requires valid agent API key - skipping for now)"

# Test 5: Create Order
echo -e "\n${YELLOW}Test 5: Create Order${NC}"
echo "POST $BASE_URL$AGENT_PREFIX/orders/create"
echo "   (Requires valid product data - skipping for now)"

# Test 6: Initiate Payment
echo -e "\n${YELLOW}Test 6: Initiate Payment${NC}"
echo "POST $BASE_URL$AGENT_PREFIX/payments"
echo "   (Requires valid order_id - skipping for now)"

# Test 7: Rate Limits
echo -e "\n${YELLOW}Test 7: Rate Limits${NC}"
echo "GET $BASE_URL$AGENT_PREFIX/rate-limits"
RATE_LIMITS=$(curl -s "$BASE_URL$AGENT_PREFIX/rate-limits" \
  -H "X-API-Key: $API_KEY")

if echo "$RATE_LIMITS" | grep -q '"rate_limits"'; then
    echo -e "${GREEN}✅ Rate limits check passed${NC}"
    echo "$RATE_LIMITS" | python3 -m json.tool | head -15
else
    echo -e "${RED}❌ Rate limits check failed${NC}"
    echo "$RATE_LIMITS"
fi

echo -e "\n======================================================"
echo "     ✅ Testing Complete"
echo "======================================================"
echo -e "\n${YELLOW}Next Steps:${NC}"
echo "1. Check failed tests and fix endpoints"
echo "2. Generate OpenAPI spec: curl $BASE_URL/openapi.json"
echo "3. Test with Postman using OpenAPI import"
echo "4. Generate SDK from OpenAPI spec"


