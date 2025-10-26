#!/bin/bash

echo "🔍 Checking merchant@test.com data via API"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Get merchant login token
echo -e "\n1️⃣ Logging in as merchant@test.com..."
LOGIN_RESPONSE=$(curl -s -X POST "https://web-production-fedb.up.railway.app/merchant/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","password":"password123"}')

echo "Login response:"
echo "$LOGIN_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$LOGIN_RESPONSE"

MERCHANT_TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("data",{}).get("token",""))' 2>/dev/null)

if [ -z "$MERCHANT_TOKEN" ]; then
    echo "❌ Failed to get merchant token"
    exit 1
fi

MERCHANT_ID=$(echo "$LOGIN_RESPONSE" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("data",{}).get("user",{}).get("merchant_id",""))' 2>/dev/null)

echo -e "\n✅ Logged in successfully"
echo "Merchant ID: $MERCHANT_ID"

# Get dashboard stats
echo -e "\n2️⃣ Fetching dashboard stats..."
STATS=$(curl -s -X GET "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
  -H "Authorization: Bearer $MERCHANT_TOKEN")

echo "Dashboard stats:"
echo "$STATS" | python3 -m json.tool 2>/dev/null || echo "$STATS"

# Get orders
echo -e "\n3️⃣ Fetching orders..."
ORDERS=$(curl -s -X GET "https://web-production-fedb.up.railway.app/merchant/orders" \
  -H "Authorization: Bearer $MERCHANT_TOKEN")

echo "Orders:"
echo "$ORDERS" | python3 -m json.tool 2>/dev/null | head -50 || echo "$ORDERS"

# Get PSP configs
echo -e "\n4️⃣ Getting admin token for PSP check..."
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n5️⃣ Fetching PSP configurations..."
PSPS=$(curl -s "https://web-production-fedb.up.railway.app/employee/psps/all" \
  -H "Authorization: Bearer $ADMIN_TOKEN")

echo "All PSPs:"
echo "$PSPS" | python3 -c "import json,sys; data=json.load(sys.stdin); merchant_psps=[p for p in data.get('psps',[]) if p.get('merchant_id')=='$MERCHANT_ID']; print(json.dumps(merchant_psps, indent=2))" 2>/dev/null || echo "$PSPS"

echo -e "\n" 
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
