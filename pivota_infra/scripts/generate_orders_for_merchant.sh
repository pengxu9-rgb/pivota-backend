#!/bin/bash

# Generate 5 test orders for merchant merch_6b90dc9838d5fd9c
# This script calls the admin test order generation endpoint

MERCHANT_ID="merch_6b90dc9838d5fd9c"
ORDER_COUNT=5
API_URL="https://web-production-fedb.up.railway.app"

echo "🚀 Generating $ORDER_COUNT test orders for merchant $MERCHANT_ID..."
echo ""

# You need an admin token to call this endpoint
# For now, we'll use the public admin token (in production, this should be secured)

curl -X POST "$API_URL/admin/test/create-orders/$MERCHANT_ID?count=$ORDER_COUNT" \
  -H "Content-Type: application/json" \
  | python3 -m json.tool

echo ""
echo "✅ Test orders generation completed!"
echo ""
echo "📊 Next steps:"
echo "1. Go to Merchant Portal > Analytics"
echo "2. Check if orders appear in the charts"
echo "3. Verify revenue and PSP distribution data"
