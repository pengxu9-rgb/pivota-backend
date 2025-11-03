#!/bin/bash

echo "🔍 Comparing Merchant IDs"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Try to login as merchant@test.com
echo -e "\n1️⃣ Logging in as merchant@test.com..."
LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/api/dashboard/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"merchant@test.com","password":"Admin123!"}')

echo "Login response:"
echo "$LOGIN" | python3 -m json.tool 2>/dev/null || echo "$LOGIN"

MERCHANT_ID_FROM_LOGIN=$(echo "$LOGIN" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    # Try different possible locations for merchant_id
    print(data.get("merchant_id") or data.get("user", {}).get("merchant_id") or data.get("data", {}).get("merchant_id") or "")
except:
    print("")
' 2>/dev/null)

echo -e "\nMerchant ID from login: $MERCHANT_ID_FROM_LOGIN"

# Get orders from Agent API
echo -e "\n2️⃣ Checking orders from Agent API..."
AGENT_KEY="ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
ORDERS=$(curl -s "https://web-production-fedb.up.railway.app/agent/v1/orders?limit=5" \
  -H "x-api-key: $AGENT_KEY")

echo "$ORDERS" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    orders = data.get("orders", [])
    if orders:
        first_order = orders[0]
        print(f"First order merchant_id: {first_order.get(\"merchant_id\")}")
        print(f"First order merchant_name: {first_order.get(\"merchant_name\")}")
        print(f"Total orders found: {len(orders)}")
    else:
        print("No orders found")
except Exception as e:
    print(f"Error: {e}")
'

echo -e "\n3️⃣ Summary:"
echo "If these merchant_ids are different, that'\''s the problem!"

echo -e "\n"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
