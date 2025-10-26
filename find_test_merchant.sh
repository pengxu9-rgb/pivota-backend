#!/bin/bash

echo "🔍 Finding merchant@test.com and chydantest.myshopify.com"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Get admin token
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n1️⃣ Checking all merchants for chydantest.myshopify.com..."

# Direct SQL query via admin endpoint
curl -s -X POST "https://web-production-fedb.up.railway.app/admin/debug/query" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT merchant_id, business_name, email, mcp_shop_domain, mcp_connected FROM merchant_onboarding WHERE mcp_shop_domain LIKE '\''%chydantest%'\'' OR email LIKE '\''%test.com'\''"}' 2>/dev/null | python3 -m json.tool 2>/dev/null

echo -e "\n2️⃣ Looking for orders with Agent API key..."
AGENT_KEY="ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"

# Get recent orders
ORDERS=$(curl -s "https://web-production-fedb.up.railway.app/agent/v1/orders?limit=20" \
  -H "x-api-key: $AGENT_KEY")

echo "Recent orders:"
echo "$ORDERS" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    orders = data.get("orders", [])
    print(f"\nTotal orders: {len(orders)}\n")
    for o in orders[:10]:
        print(f"Order: {o.get(\"order_id\")}")
        print(f"  Merchant: {o.get(\"merchant_id\")}")
        print(f"  Amount: ${o.get(\"total_amount\",0)/100:.2f}")
        print(f"  Status: {o.get(\"payment_status\")}")
        print(f"  PSP: {o.get(\"psp_type\")}")
        print(f"  Shopify: {o.get(\"shopify_order_id\",\"Not synced\")}")
        print()
except:
    print("Error parsing response")
' 2>/dev/null || echo "$ORDERS"

echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
