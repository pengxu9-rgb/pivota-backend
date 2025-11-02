#!/bin/bash

# Test Protocol Validation for all protocols
API_URL="https://web-production-fedb.up.railway.app"

echo "========================================="
echo "Protocol Validation Test Suite"
echo "========================================="
echo ""

# Test 1: AP2 Protocol - Valid Payload
echo "🧪 Test 1: AP2 Protocol - Valid Payload"
curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "test_order_001",
      "amount": 100.00,
      "currency": "USD",
      "merchant_id": "merchant_123"
    }
  }' | python3 -m json.tool
echo ""
echo ""

# Test 2: AP2 Protocol - Invalid (Missing Required Field)
echo "🧪 Test 2: AP2 Protocol - Invalid Payload (Missing amount)"
curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "test_order_002",
      "currency": "USD",
      "merchant_id": "merchant_123"
    }
  }' | python3 -m json.tool
echo ""
echo ""

# Test 3: ACP Protocol - Valid Payload
echo "🧪 Test 3: ACP Protocol - Valid Payload"
curl -s -X POST "$API_URL/protocols/ACP/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "agent_id": "agent_123",
      "merchant_id": "merchant_123",
      "items": [
        {
          "sku": "PROD-001",
          "name": "Test Product",
          "quantity": 2,
          "price": 50.00
        }
      ],
      "customer": {
        "email": "test@example.com",
        "name": "Test Customer"
      }
    }
  }' | python3 -m json.tool
echo ""
echo ""

# Test 4: X-402 Protocol - Valid Payload
echo "🧪 Test 4: X-402 Protocol - Valid Payload"
curl -s -X POST "$API_URL/protocols/X-402/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "transaction_id": "txn_test_001",
      "amount": 150.00,
      "currency": "USD",
      "authorization_code": "AUTH123456"
    }
  }' | python3 -m json.tool
echo ""
echo ""

echo "========================================="
echo "Protocol Validation Tests Complete"
echo "========================================="

