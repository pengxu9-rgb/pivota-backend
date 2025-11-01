#!/bin/bash

# Verify current deployment status
# Usage: ./verify_deployment.sh TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Checking LIST endpoint response format..."
echo "======================================"
curl -s -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('agents'):
    agent = data['agents'][0]
    print('📊 List endpoint fields:')
    print(f'  - request_count: {agent.get(\"request_count\", \"MISSING\")}')
    print(f'  - total_orders: {agent.get(\"total_orders\", \"MISSING\")}')
    print(f'  - total_gmv: {agent.get(\"total_gmv\", \"MISSING\")}')
    print(f'  - total_requests: {agent.get(\"total_requests\", \"MISSING\")}')
    print(f'  - merchant_count: {agent.get(\"merchant_count\", \"MISSING\")}')
    print(f'  - metrics: {\"YES\" if agent.get(\"metrics\") else \"MISSING\"}')
    print()
    if agent.get('total_orders') == 'MISSING':
        print('❌ Deployment NOT complete - total_orders field missing')
        print('   Need to wait for Railway deployment')
    else:
        print('✅ Deployment complete!')
        print(f'   Total Orders: {agent.get(\"total_orders\")}')
        print(f'   Total GMV: {agent.get(\"total_gmv\")}')
"

echo ""
echo "======================================"
echo "🔍 Checking DETAILS endpoint (should have all data)..."
echo "======================================"
curl -s -X GET "$API_URL/employee/agents/agent_ee38f2b3645a2ec2/details" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('agent'):
    agent = data['agent']
    print('📊 Details endpoint:')
    print(f'  - total_orders: {agent.get(\"total_orders\")}')
    print(f'  - total_gmv: {agent.get(\"total_gmv\")}')
    print(f'  - metrics.gmv_24h: {agent.get(\"metrics\", {}).get(\"gmv_24h\")}')
"

echo ""
echo "======================================"
echo "💡 If LIST endpoint shows MISSING fields:"
echo "   - Railway deployment still in progress"
echo "   - Wait 2-3 more minutes and re-run this script"
echo "======================================"
