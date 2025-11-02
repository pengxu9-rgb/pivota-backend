#!/bin/bash

echo "🧹 Cleanup Test Data & Verify System"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n1️⃣ Listing all merchants..."
curl -s "https://web-production-fedb.up.railway.app/admin/cleanup/list-merchants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool

echo -e "\n2️⃣ Cleanup preview (keep merch_208139f7600dbf42)..."
curl -s -X POST "https://web-production-fedb.up.railway.app/admin/cleanup/remove-other-merchants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keep_merchant_id":"merch_208139f7600dbf42","confirm":false}' | python3 -m json.tool

echo -e "\n"
read -p "⚠️  Execute cleanup? This will DELETE all other merchants! (yes/no): " CONFIRM

if [ "$CONFIRM" == "yes" ]; then
    echo -e "\n3️⃣ Executing cleanup..."
    curl -s -X POST "https://web-production-fedb.up.railway.app/admin/cleanup/remove-other-merchants" \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"keep_merchant_id":"merch_208139f7600dbf42","confirm":true}' | python3 -m json.tool
    
    echo -e "\n4️⃣ Testing Merchant Portal..."
    LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/auth/login" \
      -H "Content-Type: application/json" \
      -d '{"email":"merchant@test.com","password":"Admin123!"}')
    
    TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
    MERCHANT_ID=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("merchant_id",""))' 2>/dev/null)
    
    echo "Login Merchant ID: $MERCHANT_ID"
    
    if [ ! -z "$TOKEN" ]; then
        STATS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
          -H "Authorization: Bearer $TOKEN" | python3 -m json.tool)
        
        echo "$STATS" | python3 -c 'import json,sys; d=json.load(sys.stdin).get("data",{}); print(f"Orders: {d.get(\"total_orders\",0)}, Revenue: ${d.get(\"total_revenue\",0)}, PSPs: {d.get(\"psp_count\",0)}")'
    fi
    
    echo -e "\n5️⃣ Testing PSP Overview..."
    curl -s "https://web-production-fedb.up.railway.app/api/psp/overview?time_range=week" \
      -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool | head -50
    
else
    echo "Cleanup cancelled"
fi

echo ""
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'


