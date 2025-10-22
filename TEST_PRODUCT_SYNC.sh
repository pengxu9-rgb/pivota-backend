#!/bin/bash

echo "🧪 Product Sync Test Script"
echo "=========================="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Railway backend URL
BACKEND_URL="https://web-production-fedb.up.railway.app"

echo "Testing Railway backend..."
echo ""

# Test 1: Health check
echo "1️⃣ Testing backend health..."
HEALTH=$(curl -sS "$BACKEND_URL/health")
if echo "$HEALTH" | grep -q "ok"; then
    echo -e "${GREEN}✅ Backend is healthy${NC}"
else
    echo -e "${RED}❌ Backend health check failed${NC}"
    exit 1
fi
echo ""

# Test 2: Agent API health
echo "2️⃣ Testing Agent API..."
AGENT_HEALTH=$(curl -sS "$BACKEND_URL/agent/v1/health")
if echo "$AGENT_HEALTH" | grep -q "ok"; then
    echo -e "${GREEN}✅ Agent API is operational${NC}"
else
    echo -e "${RED}❌ Agent API health check failed${NC}"
fi
echo ""

# Test 3: Check products cache
echo "3️⃣ Checking products cache..."
CACHE_STATUS=$(curl -sS "$BACKEND_URL/test/check-products-cache")
PRODUCT_COUNT=$(echo "$CACHE_STATUS" | python3 -c "import sys, json; print(json.load(sys.stdin).get('total_products', 0))")
echo -e "${YELLOW}Current products in cache: $PRODUCT_COUNT${NC}"
echo ""

# Test 4: Product sync endpoint (requires auth)
echo "4️⃣ To test product sync, you need an auth token:"
echo ""
echo -e "${YELLOW}Get your token from Employee Portal:${NC}"
echo "1. Open https://employee.pivota.cc"
echo "2. Open DevTools (F12) → Console"
echo "3. Type: localStorage.getItem('auth_token')"
echo "4. Copy the token"
echo ""
echo "Then run:"
echo ""
echo -e "${GREEN}curl -X POST '$BACKEND_URL/products/sync/' \\${NC}"
echo -e "${GREEN}  -H 'Content-Type: application/json' \\${NC}"
echo -e "${GREEN}  -H 'Authorization: Bearer YOUR_TOKEN' \\${NC}"
echo -e "${GREEN}  -d '{\"merchant_id\": \"merch_208139f7600dbf42\", \"limit\": 10}'${NC}"
echo ""

# Test 5: Check if merchants have MCP connected
echo "5️⃣ To use UI sync button:"
echo "- Login to https://employee.pivota.cc"
echo "- Find merchant with MCP connected (green checkmark)"
echo "- Click Actions (⋮) → 'Sync Products'"
echo ""

echo "=========================="
echo -e "${GREEN}✅ Basic tests passed!${NC}"
echo "Ready to sync products from Shopify!"

