#!/bin/bash

echo "🔧 Final Merchant Fix - Merge Strategy"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
echo ""
echo "Strategy:"
echo "  Keep: merch_208139f7600dbf42 (has all the orders and chydantest shop)"
echo "  Remove: merch_6b90dc9838d5fd9c (empty merchant)"
echo "  User: merchant@test.com"
echo ""

read -p "Press Enter to continue..."

ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n1️⃣ Applying migrations..."
curl -s -X POST "https://web-production-fedb.up.railway.app/admin/migrations/apply-psp-fixes" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool

echo -e "\n2️⃣ Merging merchants..."
MERGE=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/fix/merge-merchants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "keep_merchant_id": "merch_208139f7600dbf42",
    "remove_merchant_id": "merch_6b90dc9838d5fd9c",
    "user_email": "merchant@test.com"
  }')

echo "$MERGE" | python3 -m json.tool

echo -e "\n3️⃣ Testing login..."
LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","password":"Admin123!"}')

MERCHANT_ID=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("merchant_id",""))' 2>/dev/null)

echo "Merchant ID after merge: $MERCHANT_ID"

if [ "$MERCHANT_ID" == "merch_208139f7600dbf42" ]; then
    echo "✅ SUCCESS! Merchant ID is correct!"
    
    TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
    
    echo -e "\n4️⃣ Testing dashboard..."
    STATS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
      -H "Authorization: Bearer $TOKEN")
    
    echo "$STATS" | python3 -c '
import json, sys
data = json.load(sys.stdin).get("data", {})
print(f"Orders: {data.get(\"total_orders\", 0)}")
print(f"Revenue: ${data.get(\"total_revenue\", 0)}")
print(f"Customers: {data.get(\"total_customers\", 0)}")
print(f"PSPs: {data.get(\"psp_count\", 0)}")
'
else
    echo "❌ Still wrong merchant_id"
fi

echo ""
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'


