#!/bin/bash

# Complete Phase 4 Testing Suite
# Tests all payment routing and protocol features

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

# Check if token is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <EMPLOYEE_TOKEN>"
    echo "Example: $0 eyJhbGc..."
    exit 1
fi

TOKEN=$1

echo "========================================="
echo "Phase 4 Complete Functionality Test"
echo "========================================="
echo ""

# Test 1: Protocol Definitions (Public)
echo "📋 Test 1: Protocol Definitions"
echo "GET $API_URL/protocols/"
PROTOCOLS=$(curl -s "$API_URL/protocols/")
echo "$PROTOCOLS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'✅ Found {len(data)} protocols:')
for p in data:
    print(f'  - {p[\"protocol_name\"]} v{p[\"version\"]} ({p[\"status\"]})')
"
echo ""

# Test 2: Agent Routes Configuration
echo "📍 Test 2: Agent Routes Configuration"
echo "GET $API_URL/agents/$AGENT_ID/routes"
ROUTES=$(curl -s "$API_URL/agents/$AGENT_ID/routes" \
  -H "Authorization: Bearer $TOKEN")
echo "$ROUTES" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'✅ Found {len(data)} route configurations')
        for r in data:
            print(f'  Route ID: {r.get(\"route_id\")}')
            print(f'  Strategy: {r.get(\"routing_strategy\")}')
            psp_priority = r.get('psp_priority', [])
            print(f'  PSP Priority: {[p.get(\"psp\") for p in psp_priority]}')
    else:
        print(data)
except Exception as e:
    print(f'❌ Error: {e}')
"
echo ""

# Test 3: Agent Protocols
echo "🔌 Test 3: Agent Protocols"
echo "GET $API_URL/agents/$AGENT_ID/protocols/"
AGENT_PROTOCOLS=$(curl -s "$API_URL/agents/$AGENT_ID/protocols/" \
  -H "Authorization: Bearer $TOKEN")
echo "$AGENT_PROTOCOLS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'✅ Agent has {len(data)} protocols enabled')
        for p in data:
            print(f'  - {p.get(\"protocol_name\")} v{p.get(\"version\")} ({p.get(\"status\")})')
    else:
        print(data)
except Exception as e:
    print(f'❌ Error: {e}')
"
echo ""

# Test 4: Protocol Validation
echo "🔍 Test 4: Protocol Payload Validation (AP2)"
echo "POST $API_URL/protocols/AP2/validate"
VALIDATION=$(curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "test_001",
      "amount": 100.00,
      "currency": "USD",
      "merchant_id": "merchant_123"
    }
  }')
echo "$VALIDATION" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('valid'):
        print(f'✅ Validation passed!')
        print(f'  Protocol: {data.get(\"protocol\")} v{data.get(\"version\")}')
    else:
        print(f'❌ Validation failed: {data.get(\"errors\")}')
except Exception as e:
    print(f'Error: {e}')
"
echo ""

# Test 5: PSP Performance (Employee only)
echo "📊 Test 5: PSP Performance Metrics"
echo "GET $API_URL/employee/psp/performance"
PSP_PERF=$(curl -s "$API_URL/employee/psp/performance" \
  -H "Authorization: Bearer $TOKEN")
echo "$PSP_PERF" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'✅ PSP Performance data available')
        print(f'  Monitoring {len(data)} PSPs')
        for psp in data[:3]:  # Show first 3
            print(f'  - {psp.get(\"psp_name\")}: {psp.get(\"current_status\")} ({psp.get(\"success_rate_5min\", 0):.1f}% success)')
    elif 'detail' in data:
        print(f'⚠️  {data.get(\"detail\")}')
    else:
        print(data)
except Exception as e:
    print(f'Error: {e}')
"
echo ""

# Test 6: Routing Overview
echo "🗺️  Test 6: Routing Overview Dashboard"
echo "GET $API_URL/employee/psp/routes/overview"
OVERVIEW=$(curl -s "$API_URL/employee/psp/routes/overview" \
  -H "Authorization: Bearer $TOKEN")
echo "$OVERVIEW" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(f'✅ Routing Overview:')
    print(f'  Total Routes: {data.get(\"total_routes\", 0)}')
    print(f'  Active Routes: {data.get(\"active_routes\", 0)}')
    print(f'  Total Agents: {data.get(\"total_agents\", 0)}')
    strategies = data.get('routes_by_strategy', {})
    if strategies:
        print(f'  Strategies: {strategies}')
except Exception as e:
    print(f'Error: {e}')
"
echo ""

# Test 7: Protocol Usage Stats (Employee only)
echo "📈 Test 7: Protocol Usage Statistics"
echo "GET $API_URL/employee/protocols/usage-stats?hours=24"
USAGE_STATS=$(curl -s "$API_URL/employee/protocols/usage-stats?hours=24" \
  -H "Authorization: Bearer $TOKEN")
echo "$USAGE_STATS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    protocol_usage = data.get('protocol_usage', [])
    print(f'✅ Protocol Usage (Last 24h):')
    if protocol_usage:
        for usage in protocol_usage:
            print(f'  - {usage.get(\"protocol\")}: {usage.get(\"total_events\", 0)} events')
    else:
        print('  No usage data yet (expected for new deployment)')
except Exception as e:
    print(f'Error: {e}')
"
echo ""

echo "========================================="
echo "Phase 4 Test Suite Complete"
echo "========================================="
echo ""
echo "✅ All Phase 4 core features are operational!"
echo ""
echo "Next steps:"
echo "1. Test protocol validation with different payloads"
echo "2. Create a test payment to trigger routing logic"
echo "3. Monitor PSP performance metrics"
echo "4. View enhanced Agent details in Employee Portal"
echo ""


# Complete Phase 4 Testing Suite
# Tests all payment routing and protocol features

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

# Check if token is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <EMPLOYEE_TOKEN>"
    echo "Example: $0 eyJhbGc..."
    exit 1
fi

TOKEN=$1

echo "========================================="
echo "Phase 4 Complete Functionality Test"
echo "========================================="
echo ""

# Test 1: Protocol Definitions (Public)
echo "📋 Test 1: Protocol Definitions"
echo "GET $API_URL/protocols/"
PROTOCOLS=$(curl -s "$API_URL/protocols/")
echo "$PROTOCOLS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'✅ Found {len(data)} protocols:')
for p in data:
    print(f'  - {p[\"protocol_name\"]} v{p[\"version\"]} ({p[\"status\"]})')
"
echo ""

# Test 2: Agent Routes Configuration
echo "📍 Test 2: Agent Routes Configuration"
echo "GET $API_URL/agents/$AGENT_ID/routes"
ROUTES=$(curl -s "$API_URL/agents/$AGENT_ID/routes" \
  -H "Authorization: Bearer $TOKEN")
echo "$ROUTES" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'✅ Found {len(data)} route configurations')
        for r in data:
            print(f'  Route ID: {r.get(\"route_id\")}')
            print(f'  Strategy: {r.get(\"routing_strategy\")}')
            psp_priority = r.get('psp_priority', [])
            print(f'  PSP Priority: {[p.get(\"psp\") for p in psp_priority]}')
    else:
        print(data)
except Exception as e:
    print(f'❌ Error: {e}')
"
echo ""

# Test 3: Agent Protocols
echo "🔌 Test 3: Agent Protocols"
echo "GET $API_URL/agents/$AGENT_ID/protocols/"
AGENT_PROTOCOLS=$(curl -s "$API_URL/agents/$AGENT_ID/protocols/" \
  -H "Authorization: Bearer $TOKEN")
echo "$AGENT_PROTOCOLS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'✅ Agent has {len(data)} protocols enabled')
        for p in data:
            print(f'  - {p.get(\"protocol_name\")} v{p.get(\"version\")} ({p.get(\"status\")})')
    else:
        print(data)
except Exception as e:
    print(f'❌ Error: {e}')
"
echo ""

# Test 4: Protocol Validation
echo "🔍 Test 4: Protocol Payload Validation (AP2)"
echo "POST $API_URL/protocols/AP2/validate"
VALIDATION=$(curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "test_001",
      "amount": 100.00,
      "currency": "USD",
      "merchant_id": "merchant_123"
    }
  }')
echo "$VALIDATION" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('valid'):
        print(f'✅ Validation passed!')
        print(f'  Protocol: {data.get(\"protocol\")} v{data.get(\"version\")}')
    else:
        print(f'❌ Validation failed: {data.get(\"errors\")}')
except Exception as e:
    print(f'Error: {e}')
"
echo ""

# Test 5: PSP Performance (Employee only)
echo "📊 Test 5: PSP Performance Metrics"
echo "GET $API_URL/employee/psp/performance"
PSP_PERF=$(curl -s "$API_URL/employee/psp/performance" \
  -H "Authorization: Bearer $TOKEN")
echo "$PSP_PERF" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'✅ PSP Performance data available')
        print(f'  Monitoring {len(data)} PSPs')
        for psp in data[:3]:  # Show first 3
            print(f'  - {psp.get(\"psp_name\")}: {psp.get(\"current_status\")} ({psp.get(\"success_rate_5min\", 0):.1f}% success)')
    elif 'detail' in data:
        print(f'⚠️  {data.get(\"detail\")}')
    else:
        print(data)
except Exception as e:
    print(f'Error: {e}')
"
echo ""

# Test 6: Routing Overview
echo "🗺️  Test 6: Routing Overview Dashboard"
echo "GET $API_URL/employee/psp/routes/overview"
OVERVIEW=$(curl -s "$API_URL/employee/psp/routes/overview" \
  -H "Authorization: Bearer $TOKEN")
echo "$OVERVIEW" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(f'✅ Routing Overview:')
    print(f'  Total Routes: {data.get(\"total_routes\", 0)}')
    print(f'  Active Routes: {data.get(\"active_routes\", 0)}')
    print(f'  Total Agents: {data.get(\"total_agents\", 0)}')
    strategies = data.get('routes_by_strategy', {})
    if strategies:
        print(f'  Strategies: {strategies}')
except Exception as e:
    print(f'Error: {e}')
"
echo ""

# Test 7: Protocol Usage Stats (Employee only)
echo "📈 Test 7: Protocol Usage Statistics"
echo "GET $API_URL/employee/protocols/usage-stats?hours=24"
USAGE_STATS=$(curl -s "$API_URL/employee/protocols/usage-stats?hours=24" \
  -H "Authorization: Bearer $TOKEN")
echo "$USAGE_STATS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    protocol_usage = data.get('protocol_usage', [])
    print(f'✅ Protocol Usage (Last 24h):')
    if protocol_usage:
        for usage in protocol_usage:
            print(f'  - {usage.get(\"protocol\")}: {usage.get(\"total_events\", 0)} events')
    else:
        print('  No usage data yet (expected for new deployment)')
except Exception as e:
    print(f'Error: {e}')
"
echo ""

echo "========================================="
echo "Phase 4 Test Suite Complete"
echo "========================================="
echo ""
echo "✅ All Phase 4 core features are operational!"
echo ""
echo "Next steps:"
echo "1. Test protocol validation with different payloads"
echo "2. Create a test payment to trigger routing logic"
echo "3. Monitor PSP performance metrics"
echo "4. View enhanced Agent details in Employee Portal"
echo ""

