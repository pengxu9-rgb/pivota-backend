#!/bin/bash
# Test direct order creation with different PSPs

API_URL="https://web-production-fedb.up.railway.app"
API_KEY="ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"

echo "=== Testing Adyen Order Creation ==="
curl -X POST "$API_URL/agent/v1/orders/create" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "adyen",
    "items": [{
      "product_id": "test_001",
      "product_title": "Test Product",
      "quantity": 1,
      "unit_price": 5.00,
      "subtotal": 5.00
    }],
    "customer_email": "test@test.com",
    "shipping_address": {
      "name": "Test",
      "address_line1": "123 St",
      "city": "NYC",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    },
    "subtotal": 5.00,
    "shipping_fee": 0,
    "tax": 0.40,
    "total": 5.40,
    "currency": "USD"
  }' 2>&1 | python3 -m json.tool 2>/dev/null | grep -A 3 "payment"

echo ""
echo "=== Checking Merchant PSPs in Database ==="
echo "This would require direct DB access..."


