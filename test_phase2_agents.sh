#!/bin/bash

# Test Agents Phase 2 Features
# Usage: ./test_phase2_agents.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

echo "======================================"
echo "🧪 Testing Agents Phase 2 Features"
echo "======================================"
echo ""

echo "1️⃣ Get Agent Details (should include api_keys and protocols)"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('agent'):
    agent = data['agent']
    print(f'✅ Agent: {agent.get(\"name\")}')
    print(f'✅ API Keys: {len(agent.get(\"api_keys\", []))} keys')
    print(f'✅ Protocols: {len(agent.get(\"protocols\", []))} protocols')
    if agent.get('api_keys'):
        for key in agent['api_keys']:
            print(f'  - {key.get(\"key_prefix\")} (scopes: {key.get(\"scopes\")})')
    if agent.get('protocols'):
        for proto in agent['protocols']:
            print(f'  - {proto.get(\"protocol_name\")} v{proto.get(\"version\")} ({proto.get(\"status\")})')
"

echo ""
echo ""
echo "2️⃣ List Agent API Keys"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/api-keys" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Total API Keys: {data.get(\"total\", 0)}')
for key in data.get('api_keys', []):
    print(f'  - {key.get(\"key_id\")}: {key.get(\"key_prefix\")} ({\"Active\" if key.get(\"is_active\") else \"Revoked\"})')
"

echo ""
echo ""
echo "3️⃣ Create New API Key"
echo "--------------------------------------"
echo "Creating key with scopes: [orders:read, products:read, orders:write]"
NEW_KEY_RESPONSE=$(curl -sS "$API_URL/employee/agents/$AGENT_ID/api-keys" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "scopes": ["orders:read", "products:read", "orders:write"],
    "ip_whitelist": [],
    "expires_in_days": 90
  }')

echo "$NEW_KEY_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('status') == 'success':
    print(f'✅ Created: {data.get(\"key_id\")}')
    print(f'✅ API Key (SAVE THIS): {data.get(\"api_key\")}')
    print(f'✅ Prefix: {data.get(\"key_prefix\")}')
    print(f'✅ Scopes: {data.get(\"scopes\")}')
else:
    print(f'❌ Error: {data.get(\"detail\", \"Unknown error\")}')
"

NEW_KEY_ID=$(echo "$NEW_KEY_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('key_id', ''))")

echo ""
echo ""
echo "4️⃣ List Protocols"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/protocols" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Total Protocols: {data.get(\"total\", 0)}')
for proto in data.get('protocols', []):
    print(f'  - {proto.get(\"protocol_name\")} v{proto.get(\"version\")} ({proto.get(\"status\")})')
"

echo ""
echo ""
echo "5️⃣ Add GraphQL Protocol"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/protocols" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol_name": "GraphQL",
    "version": "2023"
  }' | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('status') == 'success':
    print(f'✅ {data.get(\"message\")}')
else:
    print(f'❌ Error: {data.get(\"detail\", \"Unknown error\")}')
"

echo ""
echo ""
echo "6️⃣ Get Performance Stats"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/performance?period=7d" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('summary'):
    s = data['summary']
    print(f'✅ Total Requests: {s.get(\"total_requests\")}')
    print(f'✅ Success Rate: {s.get(\"success_rate\"):.1f}%')
    print(f'✅ Avg Latency: {s.get(\"avg_latency_ms\")}ms')
elif data.get('stats'):
    print(f'✅ Found {len(data[\"stats\"])} aggregated periods')
"

echo ""
echo ""
if [ -n "$NEW_KEY_ID" ]; then
    echo "7️⃣ Revoke API Key (cleanup test key)"
    echo "--------------------------------------"
    curl -sS -X DELETE "$API_URL/employee/agents/$AGENT_ID/api-keys/$NEW_KEY_ID" \
      -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'{data.get(\"message\", \"Done\")}')"
fi

echo ""
echo ""
echo "======================================"
echo "✅ Phase 2 Testing Complete"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Check frontend displays API keys and protocols in detail modal"
echo "2. Verify data is accurate"
echo "3. Phase 3: Add UI buttons for generate/revoke/rotate"
echo "======================================"

