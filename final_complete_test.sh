#!/bin/bash

echo "🎯 Final Complete Test - PSP Overview & Merchant Portal"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n📋 Step 1: Apply ALL migrations..."
curl -s -X POST "https://web-production-fedb.up.railway.app/admin/migrations/apply-psp-fixes" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool

echo -e "\n📋 Step 2: Merge merchants (fix merchant@test.com)..."
MERGE=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/fix/merge-merchants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "keep_merchant_id": "merch_208139f7600dbf42",
    "remove_merchant_id": "merch_6b90dc9838d5fd9c",
    "user_email": "merchant@test.com"
  }')

echo "$MERGE" | python3 -m json.tool

echo -e "\n📋 Step 3: Test Merchant Portal login..."
LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","password":"Admin123!"}')

MERCHANT_ID=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("merchant_id",""))' 2>/dev/null)
MERCHANT_TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)

echo "Merchant ID: $MERCHANT_ID"

if [ "$MERCHANT_ID" == "merch_208139f7600dbf42" ]; then
    echo "✅ Merchant Portal login SUCCESS!"
    
    echo -e "\n📋 Step 4: Test Merchant Dashboard..."
    STATS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
      -H "Authorization: Bearer $MERCHANT_TOKEN" | python3 -m json.tool)
    
    ORDERS=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("total_orders",0))' 2>/dev/null)
    REVENUE=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("total_revenue",0))' 2>/dev/null)
    PSP_COUNT=$(echo "$STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("psp_count",0))' 2>/dev/null)
    
    echo "Dashboard Stats:"
    echo "  Orders: $ORDERS"
    echo "  Revenue: \$$REVENUE"
    echo "  PSP Count: $PSP_COUNT"
    
    if [ "$ORDERS" -gt 0 ]; then
        echo "  ✅ Merchant Dashboard showing REAL DATA!"
    else
        echo "  ❌ Dashboard still showing 0"
    fi
else
    echo "❌ Merchant ID still wrong"
fi

echo -e "\n📋 Step 5: Test PSP Overview (Last 7 days)..."
PSP=$(curl -s "https://web-production-fedb.up.railway.app/api/psp/overview?time_range=week" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool)

echo "$PSP" | python3 -c '
import json, sys
data = json.load(sys.stdin)

summary = data.get("summary", {})
print(f"\nPSP Overview Summary:")
print(f"  Total PSPs: {summary.get(\"total_psps\", 0)}")
print(f"  Total Transactions: {summary.get(\"total_transactions\", 0)}")
print(f"  Total Volume: ${summary.get(\"total_volume\", 0):.2f}")
print(f"  Avg Success Rate: {summary.get(\"avg_success_rate\", 0)}%")

print(f"\nPer-PSP Stats:")
for psp in data.get("psps", []):
    print(f"  {psp[\"name\"]}:")
    print(f"    Transactions: {psp[\"transactions_today\"]}")
    print(f"    Success Rate: {psp[\"success_rate\"]}%")
    print(f"    Volume: ${psp[\"total_volume\"]:.2f}")
'

echo ""
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
echo ""
echo "✅ Test Complete!"
echo ""
echo "Expected Results:"
echo "1. Merchant Portal Dashboard shows 20+ orders and real revenue"
echo "2. PSP Overview shows accurate transaction counts per PSP"
echo "3. Success rates should be ~90%+ not 2%"
echo "4. No duplicate counting"
