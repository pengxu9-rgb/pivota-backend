#!/bin/bash

echo "======================================"
echo "🔍 检查当前 PSP 配置"
echo "======================================"
echo ""
echo "获取商户 merch_208139f7600dbf42 的所有 PSP 配置..."
echo ""

# 使用 Agent API 创建一个测试订单来查看日志
echo "1️⃣ 测试 Stripe:"
curl -sS -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "stripe",
    "items": [{
      "product_id": "test_stripe",
      "product_title": "Stripe Test",
      "quantity": 1,
      "unit_price": 0.01,
      "subtotal": 0.01
    }],
    "customer_email": "test@example.com",
    "shipping_address": {
      "name": "Test",
      "address_line1": "123 Test",
      "city": "NYC",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    }
  }' | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Status: {data.get(\"status\", \"error\")}'); print(f'  PSP: {data.get(\"psp_type\", \"N/A\")}'); print(f'  Has Secret: {\"client_secret\" in data}')"

echo ""
echo "2️⃣ 测试 Adyen:"
curl -sS -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "adyen",
    "items": [{
      "product_id": "test_adyen",
      "product_title": "Adyen Test",
      "quantity": 1,
      "unit_price": 0.01,
      "subtotal": 0.01
    }],
    "customer_email": "test@example.com",
    "shipping_address": {
      "name": "Test",
      "address_line1": "123 Test",
      "city": "NYC",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    }
  }' | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Status: {data.get(\"status\", \"error\")}'); print(f'  Error: {data.get(\"error\", \"None\")}'); print(f'  Has Secret: {\"client_secret\" in data}')"

echo ""
echo "3️⃣ 测试 Checkout:"
curl -sS -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "checkout",
    "items": [{
      "product_id": "test_checkout",
      "product_title": "Checkout Test",
      "quantity": 1,
      "unit_price": 0.01,
      "subtotal": 0.01
    }],
    "customer_email": "test@example.com",
    "shipping_address": {
      "name": "Test",
      "address_line1": "123 Test",
      "city": "NYC",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    }
  }' | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Status: {data.get(\"status\", \"error\")}'); print(f'  PSP: {data.get(\"psp_type\", \"N/A\")}'); print(f'  Has Secret: {\"client_secret\" in data}')"

echo ""
echo "4️⃣ 测试 PayPal:"
curl -sS -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "paypal",
    "items": [{
      "product_id": "test_paypal",
      "product_title": "PayPal Test",
      "quantity": 1,
      "unit_price": 0.01,
      "subtotal": 0.01
    }],
    "customer_email": "test@example.com",
    "shipping_address": {
      "name": "Test",
      "address_line1": "123 Test",
      "city": "NYC",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    }
  }' | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Status: {data.get(\"status\", \"error\")}'); print(f'  Error: {data.get(\"error\", \"None\")}'); print(f'  Has Secret: {\"client_secret\" in data}')"

echo ""
echo "======================================"
echo "✅ 测试完成"
echo "======================================"
echo ""
echo "说明："
echo "- 'success' + Has Secret: True = PSP 配置正确 ✅"
echo "- 'error' = PSP 未配置或配置错误 ❌"
echo ""
echo "如果 Adyen 或 PayPal 失败，请："
echo "1. 使用 quick_test_psps.py 测试您的 API keys"
echo "2. 在 Employee Portal 重新配置"

