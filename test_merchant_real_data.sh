#!/bin/bash

# Script to test merchant real data (no demo fallback)
# Usage: ./test_merchant_real_data.sh MERCHANT_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 MERCHANT_TOKEN"
    echo "Please provide your merchant token"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 1. Fetching merchant profile (real data only)..."
echo "======================================"
curl -s -X GET "$API_URL/merchant/profile" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "📊 2. Fetching merchant dashboard stats (real data only)..."
echo "======================================"
curl -s -X GET "$API_URL/merchant/dashboard/stats" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "📦 3. Fetching merchant orders (real data only)..."
echo "======================================"
curl -s -X GET "$API_URL/merchant/orders?limit=5" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "✅ If you see real data above (not demo values), the fix worked!"
echo "❌ If you see errors or demo data, there's still an issue."
echo ""
echo "Demo data indicators to watch for:"
echo "- business_name: 'ChydanTest Store' (demo)"
echo "- total_orders: 1250 (demo)"
echo "- total_revenue: 125000 (demo)"
echo "======================================"
