#!/bin/bash

echo "🔧 Complete Fix Flow for Merchant Portal and PSP Overview"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Get admin token
echo -e "\n1️⃣ Getting admin token..."
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

if [ -z "$ADMIN_TOKEN" ]; then
    echo "❌ Failed to get admin token"
    exit 1
fi
echo "✅ Admin token obtained"

# Apply migrations
echo -e "\n2️⃣ Applying database migrations (add merchant_id to users table)..."
MIGRATION=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/migrations/apply-psp-fixes" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json")

echo "$MIGRATION" | python3 -m json.tool 2>/dev/null || echo "$MIGRATION"

# Fix merchant@test.com merchant_id
echo -e "\n3️⃣ Fixing merchant@test.com merchant_id..."
FIX=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/fix/merchant-id" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","correct_merchant_id":"merch_208139f7600dbf42"}')

if echo "$FIX" | grep -q '"status":"success"'; then
    echo "✅ Merchant ID fixed!"
else
    echo "Response:"
    echo "$FIX" | python3 -m json.tool 2>/dev/null || echo "$FIX"
fi

# Test PSP Overview
echo -e "\n4️⃣ Testing PSP Overview API..."
PSP=$(curl -s "https://web-production-fedb.up.railway.app/api/psp/overview?time_range=today" \
  -H "Authorization: Bearer $ADMIN_TOKEN")

if echo "$PSP" | grep -q '"psps"'; then
    PSP_COUNT=$(echo "$PSP" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("psps",[])))' 2>/dev/null)
    echo "✅ PSP Overview working! Found $PSP_COUNT PSPs"
else
    echo "❌ PSP Overview failed:"
    echo "$PSP" | python3 -m json.tool 2>/dev/null | head -20
fi

# Test Merchant Login
echo -e "\n5️⃣ Testing merchant@test.com login..."
LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","password":"Admin123!"}')

if echo "$LOGIN" | grep -q '"success":true'; then
    MERCHANT_TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
    MERCHANT_ID=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("merchant_id",""))' 2>/dev/null)
    
    echo "✅ Login successful!"
    echo "   Merchant ID: $MERCHANT_ID"
    
    # Test Dashboard
    echo -e "\n6️⃣ Testing Merchant Dashboard..."
    STATS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
      -H "Authorization: Bearer $MERCHANT_TOKEN")
    
    ORDERS=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("total_orders",0))' 2>/dev/null)
    REVENUE=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("total_revenue",0))' 2>/dev/null)
    
    if [ "$ORDERS" -gt 0 ]; then
        echo "✅ Dashboard showing real data!"
        echo "   Orders: $ORDERS"
        echo "   Revenue: \$$REVENUE"
    else
        echo "⚠️  Dashboard still showing 0"
        echo "$STATS" | python3 -m json.tool 2>/dev/null | head -20
    fi
else
    echo "❌ Login failed"
fi

echo -e "\n"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
echo "✅ Fix flow complete!"
echo ""
echo "Summary:"
echo "- PSP Overview should show all configured PSPs with metrics"
echo "- Merchant Portal should show merchant@test.com's 20+ orders"
echo "- Both pages should display real data, not 0s"

