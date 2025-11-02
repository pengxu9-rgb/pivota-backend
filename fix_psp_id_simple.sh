#!/bin/bash

echo "🔧 简单方法：为所有订单均匀分配 PSP ID"
echo "=========================================="

API_URL="https://web-production-fedb.up.railway.app"

# 获取订单 IDs
echo "步骤 1: 获取所有订单 ID..."

# 方法：直接用序列号分配
# 每 4 个订单循环分配给 4 个 PSP

echo ""
echo "步骤 2: 更新前 5 个订单为 Stripe..."
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "WITH numbered AS (SELECT order_id, ROW_NUMBER() OVER (ORDER BY created_at) as rn FROM orders WHERE merchant_id = '\''merch_208139f7600dbf42'\'') UPDATE orders SET psp_id = '\''psp_stripe_031421904229'\'' FROM numbered WHERE orders.order_id = numbered.order_id AND MOD(numbered.rn::integer - 1, 4) = 0;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "步骤 3: 更新接下来的订单为 Adyen..."
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "WITH numbered AS (SELECT order_id, ROW_NUMBER() OVER (ORDER BY created_at) as rn FROM orders WHERE merchant_id = '\''merch_208139f7600dbf42'\'') UPDATE orders SET psp_id = '\''psp_adyen_031451113160'\'' FROM numbered WHERE orders.order_id = numbered.order_id AND MOD(numbered.rn::integer - 1, 4) = 1;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "步骤 4: 更新为 Checkout..."
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "WITH numbered AS (SELECT order_id, ROW_NUMBER() OVER (ORDER BY created_at) as rn FROM orders WHERE merchant_id = '\''merch_208139f7600dbf42'\'') UPDATE orders SET psp_id = '\''psp_checkout_031532400804'\'' FROM numbered WHERE orders.order_id = numbered.order_id AND MOD(numbered.rn::integer - 1, 4) = 2;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "步骤 5: 更新为 PayPal..."
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "WITH numbered AS (SELECT order_id, ROW_NUMBER() OVER (ORDER BY created_at) as rn FROM orders WHERE merchant_id = '\''merch_208139f7600dbf42'\'') UPDATE orders SET psp_id = '\''psp_paypal_031608975828'\'' FROM numbered WHERE orders.order_id = numbered.order_id AND MOD(numbered.rn::integer - 1, 4) = 3;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "✅ 完成！"

