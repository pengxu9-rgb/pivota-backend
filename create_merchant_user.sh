#!/bin/bash

echo "👤 Creating user account for merchant@test.com"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

MERCHANT_ID="merch_208139f7600dbf42"
EMAIL="merchant@test.com"
PASSWORD="Test123!"

# Get admin token
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n1️⃣ Creating merchant user via API..."

# Try to create user via signup
SIGNUP=$(curl -s -X POST "https://web-production-fedb.up.railway.app/auth/signup" \
  -H "Content-Type: application/json" \
  -d "{
    \"email\": \"$EMAIL\",
    \"password\": \"$PASSWORD\",
    \"role\": \"merchant\",
    \"merchant_id\": \"$MERCHANT_ID\"
  }")

echo "Signup response:"
echo "$SIGNUP" | python3 -m json.tool 2>/dev/null || echo "$SIGNUP"

echo -e "\n2️⃣ Testing login..."

LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/dashboard/auth/login" \
  -H "Content-Type: application/json" \
  -d "{
    \"username\": \"$EMAIL\",
    \"password\": \"$PASSWORD\"
  }")

echo "Login response:"
echo "$LOGIN" | python3 -m json.tool 2>/dev/null || echo "$LOGIN"

TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("access_token",""))' 2>/dev/null)

if [ ! -z "$TOKEN" ]; then
    echo -e "\n✅ Login successful!"
    echo "Token: ${TOKEN:0:50}..."
    
    echo -e "\n3️⃣ Fetching orders..."
    ORDERS=$(curl -s "https://web-production-fedb.up.railway.app/api/dashboard/orders?limit=5" \
      -H "Authorization: Bearer $TOKEN")
    
    echo "$ORDERS" | python3 -m json.tool 2>/dev/null | head -30 || echo "$ORDERS"
else
    echo -e "\n❌ Login failed"
fi

echo -e "\n"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
