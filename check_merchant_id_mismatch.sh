#!/bin/bash

echo "🔍 Checking merchant_id mismatch"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

echo -e "\n1️⃣ Finding merchant@test.com's merchant_id..."
MERCHANT_INFO=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/debug/query" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT merchant_id, business_name, email, contact_email FROM merchant_onboarding WHERE email = '\''merchant@test.com'\'' OR contact_email = '\''merchant@test.com'\''"}')

echo "$MERCHANT_INFO" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    if "results" in data:
        for row in data["results"]:
            print(f"Merchant ID: {row.get(\"merchant_id\")}")
            print(f"Business: {row.get(\"business_name\")}")
            print(f"Email: {row.get(\"email\")}")
            print(f"Contact Email: {row.get(\"contact_email\")}")
    else:
        print("Query failed or returned no data")
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")
' 2>/dev/null || echo "$MERCHANT_INFO"

echo -e "\n2️⃣ Finding chydantest.myshopify.com merchant_id..."
SHOP_INFO=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/debug/query" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT merchant_id, business_name, mcp_shop_domain FROM merchant_onboarding WHERE mcp_shop_domain LIKE '\''%chydantest%'\''"}')

echo "$SHOP_INFO" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    if "results" in data:
        for row in data["results"]:
            print(f"Merchant ID: {row.get(\"merchant_id\")}")
            print(f"Business: {row.get(\"business_name\")}")
            print(f"Shop Domain: {row.get(\"mcp_shop_domain\")}")
    else:
        print("Query failed or returned no data")
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")
' 2>/dev/null || echo "$SHOP_INFO"

echo -e "\n3️⃣ Finding orders merchant_id (from recent orders)..."
ORDERS_INFO=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/debug/query" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT DISTINCT merchant_id, COUNT(*) as order_count FROM orders GROUP BY merchant_id ORDER BY order_count DESC LIMIT 5"}')

echo "$ORDERS_INFO" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    if "results" in data:
        print("\nMerchants with orders:")
        for row in data["results"]:
            print(f"  Merchant ID: {row.get(\"merchant_id\")} - {row.get(\"order_count\")} orders")
    else:
        print("Query failed or returned no data")
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")
' 2>/dev/null || echo "$ORDERS_INFO"

echo -e "\n4️⃣ Checking user table for merchant@test.com..."
USER_INFO=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/debug/query" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT id, email, role FROM users WHERE email = '\''merchant@test.com'\''"}')

echo "$USER_INFO" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    if "results" in data and len(data["results"]) > 0:
        for row in data["results"]:
            print(f"User ID: {row.get(\"id\")}")
            print(f"Email: {row.get(\"email\")}")
            print(f"Role: {row.get(\"role\")}")
    else:
        print("No user found in users table")
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")
' 2>/dev/null || echo "$USER_INFO"

echo -e "\n"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
