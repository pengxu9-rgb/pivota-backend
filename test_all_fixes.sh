#!/bin/bash

echo "🧪 Complete System Test After Fixes"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Wait for deployment
echo -e "\n⏰ Waiting for Railway deployment (30 seconds)..."
sleep 30

# Get admin token
echo -e "\n${YELLOW}1️⃣ Getting admin token...${NC}"
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

if [ -z "$ADMIN_TOKEN" ]; then
    echo -e "${RED}❌ Failed to get admin token${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Admin token obtained${NC}"

# Test PSP Overview API
echo -e "\n${YELLOW}2️⃣ Testing PSP Overview API...${NC}"
PSP_RESPONSE=$(curl -s "https://web-production-fedb.up.railway.app/api/psp/overview?time_range=today" \
  -H "Authorization: Bearer $ADMIN_TOKEN")

if echo "$PSP_RESPONSE" | grep -q '"psps"'; then
    PSP_COUNT=$(echo "$PSP_RESPONSE" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("psps",[])))' 2>/dev/null)
    echo -e "${GREEN}✅ PSP Overview API working! Found $PSP_COUNT PSPs${NC}"
    echo "$PSP_RESPONSE" | python3 -m json.tool 2>/dev/null | head -40
else
    echo -e "${RED}❌ PSP Overview API failed${NC}"
    echo "$PSP_RESPONSE" | python3 -m json.tool 2>/dev/null | head -20
fi

# Fix merchant@test.com merchant_id
echo -e "\n${YELLOW}3️⃣ Fixing merchant@test.com merchant_id...${NC}"
FIX_RESPONSE=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/fix/merchant-id" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","correct_merchant_id":"merch_208139f7600dbf42"}')

if echo "$FIX_RESPONSE" | grep -q '"status":"success"'; then
    echo -e "${GREEN}✅ Merchant ID fixed!${NC}"
    echo "$FIX_RESPONSE" | python3 -m json.tool 2>/dev/null
else
    echo -e "${RED}❌ Failed to fix merchant ID${NC}"
    echo "$FIX_RESPONSE" | python3 -m json.tool 2>/dev/null
fi

# Test merchant login
echo -e "\n${YELLOW}4️⃣ Testing merchant@test.com login...${NC}"
LOGIN_RESPONSE=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","password":"Admin123!"}')

if echo "$LOGIN_RESPONSE" | grep -q '"success":true'; then
    echo -e "${GREEN}✅ Login successful!${NC}"
    MERCHANT_TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
    MERCHANT_ID=$(echo "$LOGIN_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("merchant_id",""))' 2>/dev/null)
    echo "   Merchant ID: $MERCHANT_ID"
    
    # Test dashboard stats
    echo -e "\n${YELLOW}5️⃣ Testing dashboard stats...${NC}"
    STATS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
      -H "Authorization: Bearer $MERCHANT_TOKEN")
    
    if echo "$STATS" | grep -q '"total_orders"'; then
        ORDERS=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("total_orders",0))' 2>/dev/null)
        REVENUE=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("total_revenue",0))' 2>/dev/null)
        PSP_COUNT=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("psp_count",0))' 2>/dev/null)
        
        if [ "$ORDERS" -gt 0 ]; then
            echo -e "${GREEN}✅ Dashboard showing real data!${NC}"
            echo "   Total Orders: $ORDERS"
            echo "   Total Revenue: \$$REVENUE"
            echo "   PSP Count: $PSP_COUNT"
        else
            echo -e "${YELLOW}⚠️  Dashboard still showing 0${NC}"
            echo "$STATS" | python3 -m json.tool 2>/dev/null | head -20
        fi
    else
        echo -e "${RED}❌ Dashboard API failed${NC}"
        echo "$STATS" | python3 -m json.tool 2>/dev/null | head -20
    fi
else
    echo -e "${RED}❌ Login failed${NC}"
    echo "$LOGIN_RESPONSE" | python3 -m json.tool 2>/dev/null
fi

echo -e "\n"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
echo -e "\n${GREEN}✅ Test Complete!${NC}"
echo -e "\nNext steps:"
echo "1. Refresh Employee Portal → PSP Overview page"
echo "2. Login to Merchant Portal with merchant@test.com / Admin123!"
echo "3. Both should show real data now!"


