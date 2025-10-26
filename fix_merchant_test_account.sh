#!/bin/bash

echo "⏰ Waiting for Railway deployment (20 seconds)..."
sleep 20

echo ""
echo "🔧 Fixing merchant@test.com account"
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'

# Get admin token
ADMIN_TOKEN=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

if [ -z "$ADMIN_TOKEN" ]; then
    echo "❌ Failed to get admin token"
    exit 1
fi

echo "✅ Admin token obtained"
echo ""

# Fix merchant_id
echo "🔧 Updating merchant@test.com to use merchant_id: merch_208139f7600dbf42"
FIX_RESPONSE=$(curl -s -X POST "https://web-production-fedb.up.railway.app/admin/fix/merchant-id" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "merchant@test.com",
    "correct_merchant_id": "merch_208139f7600dbf42"
  }')

echo "$FIX_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$FIX_RESPONSE"

echo ""
echo "=" | awk '{for(i=0;i<80;i++)printf "="}; END{print ""}'
echo ""
echo "✅ Done! Now try logging into Merchant Portal:"
echo "   Email: merchant@test.com"
echo "   Password: Admin123!"
echo ""
echo "   Dashboard should show:"
echo "   - 20+ orders"
echo "   - ~\$1000+ revenue"
echo "   - Real PSP count"
