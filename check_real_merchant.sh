#!/bin/bash

MERCHANT_ID="merch_208139f7600dbf42"
EMAIL="yao.wang@chydan.com"

echo "🔍 Checking merchant: $EMAIL ($MERCHANT_ID)"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Try to login
echo -e "\n1️⃣ Attempting login..."
LOGIN=$(curl -s -X POST "https://web-production-fedb.up.railway.app/merchant/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Merchant123!\"}")

echo "$LOGIN" | python3 -m json.tool 2>/dev/null || echo "$LOGIN"

TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("data",{}).get("token","") or d.get("token",""))' 2>/dev/null)

if [ -z "$TOKEN" ]; then
    echo "⚠️  Login failed or no token"
else
    echo -e "\n✅ Logged in successfully"
    
    # Get orders
    echo -e "\n2️⃣ Fetching orders..."
    ORDERS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/orders?limit=20" \
      -H "Authorization: Bearer $TOKEN")
    
    ORDER_COUNT=$(echo "$ORDERS" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("orders",[])))' 2>/dev/null)
    
    echo "Orders found: $ORDER_COUNT"
    echo "$ORDERS" | python3 -c '
import json, sys
data = json.load(sys.stdin)
orders = data.get("orders", [])[:5]
for o in orders:
    print(f"  - {o.get(\"order_id\")}: ${o.get(\"total_amount\",0)/100:.2f} - {o.get(\"payment_status\")} - Shopify: {o.get(\"shopify_order_id\",\"N/A\")}")
' 2>/dev/null
    
    # Get dashboard stats
    echo -e "\n3️⃣ Fetching dashboard stats..."
    STATS=$(curl -s "https://web-production-fedb.up.railway.app/merchant/dashboard/stats" \
      -H "Authorization: Bearer $TOKEN")
    
    echo "$STATS" | python3 -m json.tool 2>/dev/null || echo "$STATS"
fi

echo -e "\n"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
