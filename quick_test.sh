#!/bin/bash

echo "Testing payment creation..."

# Test 1: Default PSP
echo -e "\n=== Test 1: Default PSP (should use Stripe from env) ==="
curl -s -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "items": [{"product_id": "test", "product_title": "Test", "quantity": 1, "unit_price": 10, "subtotal": 10}],
    "customer_email": "test@test.com",
    "shipping_address": {"name": "Test", "address_line1": "123 St", "city": "NYC", "state": "NY", "postal_code": "10001", "country": "US"}
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"Order: {d['order_id']}, Payment Intent: {d['payment'].get('payment_intent_id', 'NULL')}\")"

echo -e "\n=== Check if order has payment_intent in DB ==="
curl -s "https://web-production-fedb.up.railway.app/agent/v1/orders?limit=1" \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" | \
  python3 -c "import sys,json; orders=json.load(sys.stdin).get('orders',[]); print(f\"Latest order: {orders[0]['order_id'] if orders else 'None'}, PI: {orders[0].get('payment_intent_id', 'NULL') if orders else 'N/A'}\")"

echo -e "\n=== Summary ==="
echo "If payment_intent is NULL, check Railway logs for:"
echo "1. 'No stripe API key found'"
echo "2. 'Payment intent creation failed'"
echo "3. Any error messages"

