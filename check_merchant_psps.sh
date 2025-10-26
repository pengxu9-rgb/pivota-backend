#!/bin/bash
# Check merchant PSP configuration

MERCHANT_ID="merch_208139f7600dbf42"

echo "=== Checking PSP Configuration for Merchant ==="
echo "Merchant ID: $MERCHANT_ID"
echo ""

# Try different endpoints to check PSP config
echo "1. Checking via direct SQL query (if we had DB access)..."
echo "   SELECT provider, api_key, account_id, secret_key, status"
echo "   FROM merchant_psps"
echo "   WHERE merchant_id = '$MERCHANT_ID'"
echo ""

echo "2. Testing each PSP to see which ones work..."
echo ""

for PSP in stripe adyen checkout paypal; do
    echo "Testing $PSP..."
    curl -sS -X POST "https://web-production-fedb.up.railway.app/agent/v1/orders/create" \
      -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
      -H "Content-Type: application/json" \
      -d "{
        \"merchant_id\": \"$MERCHANT_ID\",
        \"preferred_psp\": \"$PSP\",
        \"items\": [{
          \"product_id\": \"test\",
          \"product_title\": \"Test\",
          \"quantity\": 1,
          \"unit_price\": 1.00,
          \"subtotal\": 1.00
        }],
        \"customer_email\": \"test@test.com\",
        \"shipping_address\": {
          \"name\": \"Test\",
          \"address_line1\": \"123 St\",
          \"city\": \"NYC\",
          \"state\": \"NY\",
          \"postal_code\": \"10001\",
          \"country\": \"US\"
        },
        \"subtotal\": 1.00,
        \"shipping_fee\": 0,
        \"tax\": 0.08,
        \"total\": 1.08,
        \"currency\": \"USD\"
      }" | python3 -c "import sys, json; data=json.load(sys.stdin); payment=data.get('payment',{}); print(f'  Payment Intent: {payment.get(\"payment_intent_id\") or \"NULL\"}'); print(f'  Client Secret: {\"YES\" if payment.get(\"client_secret\") else \"NULL\"}')"
    echo ""
done

echo "=== Summary ==="
echo "If Payment Intent is NULL, the PSP is not configured in database"

