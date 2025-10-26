#!/bin/bash

echo "📋 Listing all merchants in database"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Get admin token
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

if [ -z "$ADMIN_TOKEN" ]; then
    echo "❌ Failed to get admin token"
    exit 1
fi

echo -e "\n✅ Admin token obtained"

# Get all merchants
echo -e "\n📊 Fetching all merchants..."
MERCHANTS=$(curl -s "https://web-production-fedb.up.railway.app/employee/merchants" \
  -H "Authorization: Bearer $ADMIN_TOKEN")

echo "$MERCHANTS" | python3 -c '
import json, sys
data = json.load(sys.stdin)
merchants = data.get("merchants", [])

print(f"\nTotal merchants: {len(merchants)}\n")

if merchants:
    for i, m in enumerate(merchants, 1):
        print(f"{i}. Merchant ID: {m.get(\"merchant_id\")}")
        print(f"   Business: {m.get(\"business_name\", \"N/A\")}")
        print(f"   Email: {m.get(\"email\", \"N/A\")}")
        print(f"   Status: {m.get(\"kyb_status\", \"N/A\")}")
        print(f"   MCP: {m.get(\"mcp_platform\", \"N/A\")} - {\"Connected\" if m.get(\"mcp_connected\") else \"Not Connected\"}")
        print()
else:
    print("No merchants found!")
' 2>/dev/null || echo "$MERCHANTS"

echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
