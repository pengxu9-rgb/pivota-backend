#!/bin/bash
# Test admin PSP save API directly

API_URL="https://web-production-fedb.up.railway.app"

echo "=== Testing Admin PSP Connect API ==="
echo ""
echo "Note: This test requires a valid admin token."
echo "We don't have one, so this will fail with 401/403."
echo "But we can see if the endpoint is accessible."
echo ""

# Try to save Adyen (will fail due to auth, but we can see the error)
echo "Testing POST /admin/psp/connect (Adyen)..."
curl -X POST "$API_URL/admin/psp/connect" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy_token" \
  -d '{
    "provider": "adyen",
    "merchant_id": "merch_208139f7600dbf42",
    "api_key": "test_key_12345678",
    "account_id": "TestMerchant"
  }' 2>&1 | python3 -m json.tool 2>/dev/null || echo "(Auth required as expected)"

echo ""
echo "=== Checking Current PSP Config ==="
bash check_merchant_psps.sh | grep -A 2 "Testing"


