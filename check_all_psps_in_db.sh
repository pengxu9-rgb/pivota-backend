#!/bin/bash
# Check if PSP configs exist for ANY merchant

echo "=== Checking if Adyen/Checkout/PayPal configs exist ANYWHERE in database ==="
echo ""

# We'll test with different merchants to see if configs were saved elsewhere

MERCHANTS=("merch_208139f7600dbf42" "merch_test" "test_merchant")

for MERCHANT in "${MERCHANTS[@]}"; do
    echo "Testing merchant: $MERCHANT"
    echo "  Stripe:"
    curl -sS -X POST "https://web-production-fedb.up.railway.app/agent/v1/orders/create" \
      -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
      -H "Content-Type: application/json" \
      -d "{\"merchant_id\":\"$MERCHANT\",\"preferred_psp\":\"stripe\",\"items\":[{\"product_id\":\"t\",\"product_title\":\"T\",\"quantity\":1,\"unit_price\":1,\"subtotal\":1}],\"customer_email\":\"t@t.com\",\"shipping_address\":{\"name\":\"T\",\"address_line1\":\"1\",\"city\":\"NYC\",\"state\":\"NY\",\"postal_code\":\"10001\",\"country\":\"US\"},\"subtotal\":1,\"shipping_fee\":0,\"tax\":0.08,\"total\":1.08,\"currency\":\"USD\"}" 2>&1 | python3 -c "import sys,json;d=json.load(sys.stdin);p=d.get('payment',{});print('    ✅ Has payment' if p.get('payment_intent_id') else '    ❌ No payment')" 2>/dev/null || echo "    ❌ Error"
    
    echo "  Adyen:"
    curl -sS -X POST "https://web-production-fedb.up.railway.app/agent/v1/orders/create" \
      -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
      -H "Content-Type: application/json" \
      -d "{\"merchant_id\":\"$MERCHANT\",\"preferred_psp\":\"adyen\",\"items\":[{\"product_id\":\"t\",\"product_title\":\"T\",\"quantity\":1,\"unit_price\":1,\"subtotal\":1}],\"customer_email\":\"t@t.com\",\"shipping_address\":{\"name\":\"T\",\"address_line1\":\"1\",\"city\":\"NYC\",\"state\":\"NY\",\"postal_code\":\"10001\",\"country\":\"US\"},\"subtotal\":1,\"shipping_fee\":0,\"tax\":0.08,\"total\":1.08,\"currency\":\"USD\"}" 2>&1 | python3 -c "import sys,json;d=json.load(sys.stdin);p=d.get('payment',{});print('    ✅ Has payment' if p.get('payment_intent_id') else '    ❌ No payment')" 2>/dev/null || echo "    ❌ Error"
    echo ""
done

echo "=== If all show 'No payment' for Adyen/Checkout/PayPal, configs are NOT in database ==="

