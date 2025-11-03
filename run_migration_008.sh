#!/bin/bash

# Run Migration 008 - Agents Phase 2
# Usage: ./run_migration_008.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Step 1: Check migration status"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-008-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
read -p "Continue with migration? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Migration cancelled"
    exit 0
fi

echo ""
echo "======================================"
echo "🚀 Step 2: Running migration 008..."
echo "======================================"
curl -sS -X POST "$API_URL/admin/migrations/run-008-agents-phase2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "✅ Step 3: Verify migration"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-008-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "📊 Step 4: Check agent data"
echo "======================================"
curl -sS "$API_URL/employee/agents/agent_ee38f2b3645a2ec2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('agent'):
    agent = data['agent']
    print(f\"Agent: {agent.get('name')}\")
    print(f\"API Keys: {len(agent.get('api_keys', []))}\")
    print(f\"Protocols: {len(agent.get('protocols', []))}\")
    print()
    if agent.get('api_keys'):
        print('API Keys:')
        for key in agent['api_keys']:
            print(f\"  - {key.get('key_prefix')} (scopes: {', '.join(key.get('scopes', []))})\")
    if agent.get('protocols'):
        print('Protocols:')
        for proto in agent['protocols']:
            print(f\"  - {proto.get('protocol_name')} v{proto.get('version')} ({proto.get('status')})\")
"

echo ""
echo "======================================"
echo "✅ Migration 008 Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Refresh Employee Portal and check agent details"
echo "2. Should see API Keys and Protocols sections"
echo "3. Run ./test_phase2_agents.sh to test all new endpoints"
echo "======================================"


# Run Migration 008 - Agents Phase 2
# Usage: ./run_migration_008.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Step 1: Check migration status"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-008-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
read -p "Continue with migration? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Migration cancelled"
    exit 0
fi

echo ""
echo "======================================"
echo "🚀 Step 2: Running migration 008..."
echo "======================================"
curl -sS -X POST "$API_URL/admin/migrations/run-008-agents-phase2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "✅ Step 3: Verify migration"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-008-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "📊 Step 4: Check agent data"
echo "======================================"
curl -sS "$API_URL/employee/agents/agent_ee38f2b3645a2ec2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('agent'):
    agent = data['agent']
    print(f\"Agent: {agent.get('name')}\")
    print(f\"API Keys: {len(agent.get('api_keys', []))}\")
    print(f\"Protocols: {len(agent.get('protocols', []))}\")
    print()
    if agent.get('api_keys'):
        print('API Keys:')
        for key in agent['api_keys']:
            print(f\"  - {key.get('key_prefix')} (scopes: {', '.join(key.get('scopes', []))})\")
    if agent.get('protocols'):
        print('Protocols:')
        for proto in agent['protocols']:
            print(f\"  - {proto.get('protocol_name')} v{proto.get('version')} ({proto.get('status')})\")
"

echo ""
echo "======================================"
echo "✅ Migration 008 Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Refresh Employee Portal and check agent details"
echo "2. Should see API Keys and Protocols sections"
echo "3. Run ./test_phase2_agents.sh to test all new endpoints"
echo "======================================"

