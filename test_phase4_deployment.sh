#!/bin/bash

# Test Phase 4 Deployment
echo "========================================="
echo "Phase 4 Deployment Verification"
echo "========================================="

API_URL="https://web-production-fedb.up.railway.app"

# Wait for Railway deployment to complete
echo ""
echo "⏳ Waiting for Railway deployment to complete (60 seconds)..."
sleep 60

# Test 1: Check if API is responding
echo ""
echo "📡 Testing API Health..."
HEALTH_CHECK=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/")
if [ "$HEALTH_CHECK" -eq 200 ] || [ "$HEALTH_CHECK" -eq 307 ]; then
    echo "✅ API is responding (Status: $HEALTH_CHECK)"
else
    echo "❌ API is not responding (Status: $HEALTH_CHECK)"
    echo "Deployment may still be in progress. Try again in a minute."
    exit 1
fi

# Test 2: Check Protocol Definitions endpoint
echo ""
echo "🔌 Testing Protocol Definitions endpoint..."
PROTOCOLS=$(curl -s -w "\n%{http_code}" "$API_URL/protocols" \
  -H "Content-Type: application/json")

HTTP_CODE=$(echo "$PROTOCOLS" | tail -n1)
RESPONSE=$(echo "$PROTOCOLS" | head -n -1)

if [ "$HTTP_CODE" -eq 200 ]; then
    echo "✅ Protocol definitions endpoint working!"
    echo "Protocols available:"
    echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data:
    print(f'  - {p.get(\"protocol_name\")} v{p.get(\"version\")} ({p.get(\"status\")})')
" 2>/dev/null || echo "$RESPONSE"
else
    echo "❌ Protocol endpoint returned status: $HTTP_CODE"
    echo "Response: $RESPONSE"
fi

# Test 3: Check Employee PSP Performance endpoint (requires auth)
echo ""
echo "📊 Testing PSP Performance endpoint structure..."
PSP_CHECK=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/employee/psp/performance")
if [ "$PSP_CHECK" -eq 401 ] || [ "$PSP_CHECK" -eq 403 ]; then
    echo "✅ PSP Performance endpoint exists (requires authentication)"
elif [ "$PSP_CHECK" -eq 200 ]; then
    echo "✅ PSP Performance endpoint accessible"
else
    echo "⚠️  PSP Performance endpoint returned: $PSP_CHECK"
fi

# Test 4: Check if routing endpoints are registered
echo ""
echo "🔄 Testing Payment Routing endpoint structure..."
ROUTE_CHECK=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/agents/test/routes")
if [ "$ROUTE_CHECK" -eq 401 ] || [ "$ROUTE_CHECK" -eq 403 ]; then
    echo "✅ Payment Routing endpoints registered (requires authentication)"
elif [ "$ROUTE_CHECK" -eq 404 ]; then
    echo "⚠️  Routing endpoints may not be properly registered"
else
    echo "📍 Routing endpoint status: $ROUTE_CHECK"
fi

echo ""
echo "========================================="
echo "Deployment Verification Complete"
echo "========================================="
echo ""
echo "Next Steps:"
echo "1. Run the database migration:"
echo "   curl -X POST $API_URL/admin/migrations/run/010 \\"
echo "     -H \"Authorization: Bearer YOUR_ADMIN_TOKEN\" \\"
echo "     -H \"Content-Type: application/json\""
echo ""
echo "2. Test with authentication:"
echo "   - Use your admin/employee token to test protected endpoints"
echo "   - Verify protocol testing works in Employee Portal"
echo ""
echo "3. Monitor Railway logs for any runtime errors:"
echo "   - Check https://railway.app for deployment logs"
echo ""
