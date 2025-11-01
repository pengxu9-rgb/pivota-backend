#!/bin/bash

# Script to fix agent metrics to calculate from orders table (like merchant does)
# Usage: ./fix_agent_metrics_from_orders.sh TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    echo "Please provide your admin token"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 1. Checking agent's actual orders data..."
echo "======================================"
curl -s -X GET "$API_URL/admin/fix/agent-orders-check?agent_id=agent_ee38f2b3645a2ec2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "🔧 2. Fixing agent metrics calculation (using orders table)..."
echo "======================================"
curl -s -X POST "$API_URL/admin/fix/agent-metrics-v2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "✅ 3. Checking updated agent data..."
echo "======================================"
curl -s -X GET "$API_URL/employee/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('agents'):
    agent = data['agents'][0]
    print(f\"Agent: {agent.get('agent_name', 'N/A')}\")
    print(f\"Total Requests: {agent.get('total_requests', 0)}\")
    print(f\"Total Orders: {agent.get('total_orders', 0)}\")
    print(f\"Total GMV: {agent.get('total_gmv', 0)}\")
    if agent.get('metrics'):
        print(f\"\\nMetrics (24h):\")
        print(f\"  Requests: {agent['metrics'].get('requests_24h', 0)}\")
        print(f\"  GMV: {agent['metrics'].get('total_gmv', 0)}\")
        print(f\"  Orders: {agent['metrics'].get('total_orders', 0)}\")
"

echo ""
echo "======================================"
echo "📊 Fix complete! The problem was:"
echo "======================================"
echo "❌ Before: Agent metrics calculated from agent_usage_logs (API call logs)"
echo "✅ After:  Agent metrics calculated from orders table (actual orders)"
echo ""
echo "This now matches how merchant metrics are calculated."
echo "Refresh the Employee Portal to see the correct data!"
echo "======================================" 
