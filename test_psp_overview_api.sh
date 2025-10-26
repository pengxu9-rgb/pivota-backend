#!/bin/bash

echo "🧪 Testing PSP Overview API"
echo "======================================"
echo ""

# Get admin token
echo "1️⃣ Getting admin token..."
TOKEN_RESPONSE=$(curl -s https://web-production-fedb.up.railway.app/auth/admin-token)
TOKEN=$(echo $TOKEN_RESPONSE | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)

if [ -z "$TOKEN" ]; then
    echo "❌ Failed to get token"
    exit 1
fi

echo "✅ Token obtained"
echo ""

# Wait for Railway deployment
echo "2️⃣ Waiting for Railway deployment (30 seconds)..."
sleep 30
echo ""

# Test PSP overview endpoint
echo "3️⃣ Testing /api/psp/overview..."
OVERVIEW_RESPONSE=$(curl -s -w "\nHTTP_STATUS:%{http_code}" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "https://web-production-fedb.up.railway.app/api/psp/overview?time_range=today")

HTTP_STATUS=$(echo "$OVERVIEW_RESPONSE" | grep "HTTP_STATUS" | cut -d: -f2)
BODY=$(echo "$OVERVIEW_RESPONSE" | sed '/HTTP_STATUS/d')

echo "Status Code: $HTTP_STATUS"
echo ""

if [ "$HTTP_STATUS" == "200" ]; then
    echo "✅ PSP Overview API is working!"
    echo ""
    echo "Response:"
    echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
else
    echo "❌ API returned error"
    echo "Response:"
    echo "$BODY"
fi

echo ""
echo "======================================"
echo "Test complete!"

