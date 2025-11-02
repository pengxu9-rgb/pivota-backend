#!/bin/bash

# [Phase 4++] Simple test script for dual-side routing
# Tests the API endpoints without complex imports

echo "======================================================="
echo "[Phase 4++] DUAL-SIDE ROUTING SIMPLE TEST"
echo "======================================================="

# Configuration
API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"
TEST_AGENT_ID="agent_ee38f2b3645a2ec2"
TEST_MERCHANT_ID="merchant_test_${RANDOM}"

echo "API URL: $API_BASE_URL"
echo "Test Agent: $TEST_AGENT_ID"
echo "Test Merchant: $TEST_MERCHANT_ID"
echo

# Function to make API calls
api_call() {
    local method=$1
    local endpoint=$2
    local data=$3
    
    if [ -n "$data" ]; then
        curl -s -X "$method" \
            "$API_BASE_URL$endpoint" \
            -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data"
    else
        curl -s -X "$method" \
            "$API_BASE_URL$endpoint" \
            -H "Authorization: Bearer $EMPLOYEE_TOKEN"
    fi
}

# Step 1: Run Migration 011
echo "=== Step 1: Running Migration 011 ==="
response=$(api_call POST "/admin/migrations/run-011-dual-routing")
if echo "$response" | grep -q '"status":"success"'; then
    echo "✅ Migration 011 completed successfully"
    echo "$response" | python3 -m json.tool | grep -E "(tables_created|routing_policies_count|conflict_function_exists)" || true
else
    echo "❌ Migration failed"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
fi
echo

# Step 2: Create Merchant Policy (excludes Stripe, requires Adyen)
echo "=== Step 2: Creating Merchant Policy ==="
echo "Policy: Excludes Stripe, Requires Adyen"
merchant_policy='{
  "exclude": ["stripe"],
  "prefer": ["adyen", "paypal"],
  "required": ["adyen"],
  "weights": {},
  "failover": ["square"],
  "priority": 1
}'

response=$(api_call POST "/employee/routing/policies/merchant/$TEST_MERCHANT_ID" "$merchant_policy")
if echo "$response" | grep -q '"owner_type":"merchant"'; then
    echo "✅ Merchant policy created successfully"
else
    echo "❌ Failed to create merchant policy"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
fi
echo

# Step 3: Create Agent Policy (prefers Stripe - CONFLICT!)
echo "=== Step 3: Creating Agent Policy ==="
echo "Policy: Prefers Stripe (conflicts with merchant exclusion)"
agent_policy='{
  "exclude": [],
  "prefer": ["stripe", "adyen"],
  "weights": {"stripe": 1.0, "adyen": 0.9, "paypal": 0.7},
  "failover": ["square"],
  "priority": 1
}'

response=$(api_call POST "/employee/routing/policies/agent/$TEST_AGENT_ID" "$agent_policy")
if echo "$response" | grep -q '"owner_type":"agent"'; then
    echo "✅ Agent policy created successfully"
else
    echo "❌ Failed to create agent policy"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
fi
echo

# Step 4: Simulate Routing
echo "=== Step 4: Simulating Routing Decisions ==="
simulation_request='{
  "scenarios": [
    {"amount": 100.00, "currency": "USD"},
    {"amount": 500.00, "currency": "EUR"},
    {"amount": 1000.00, "currency": "USD"}
  ]
}'

response=$(api_call POST "/employee/routing/simulate/$TEST_MERCHANT_ID/$TEST_AGENT_ID" "$simulation_request")
if echo "$response" | grep -q '"simulation_results"'; then
    echo "✅ Simulation completed"
    echo "$response" | python3 -m json.tool 2>/dev/null | grep -A 5 -B 5 "conflict" || true
    
    # Extract summary
    echo
    echo "Summary:"
    echo "$response" | python3 -m json.tool 2>/dev/null | grep -E "(total_scenarios|conflicts_detected|selected_psp)" || true
else
    echo "❌ Simulation failed"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
fi
echo

# Step 5: Get Routing Logs
echo "=== Step 5: Checking Routing Logs ==="
response=$(api_call GET "/employee/routing/logs?conflict_only=true&days=1")
if echo "$response" | grep -q '\['; then
    echo "✅ Retrieved routing logs"
    log_count=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(len(data))" 2>/dev/null || echo "0")
    echo "Found $log_count logs with conflicts"
else
    echo "❌ Failed to get routing logs"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
fi
echo

# Step 6: Get Conflict Analytics
echo "=== Step 6: Conflict Analytics ==="
response=$(api_call GET "/employee/routing/analytics/conflict-summary?days=30")
if echo "$response" | grep -q '"total_routings"'; then
    echo "✅ Analytics retrieved"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
else
    echo "❌ Failed to get analytics"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
fi
echo

# Step 7: Test DualRoutingEngine directly
echo "=== Step 7: Testing DualRoutingEngine Logic ==="
python3 - <<EOF
import sys
sys.path.insert(0, '.')
try:
    from pivota_infra.core.routing_engine import DualRoutingEngine
    
    # Test scenario: Merchant excludes Stripe, Agent prefers Stripe
    merchant_rules = {"exclude": ["stripe"], "prefer": ["adyen", "paypal"]}
    agent_rules = {"prefer": ["stripe", "adyen"], "weights": {"stripe": 1.0, "adyen": 0.9}}
    available_psps = [
        {"psp": "stripe", "priority": 1},
        {"psp": "adyen", "priority": 2},
        {"psp": "paypal", "priority": 3}
    ]
    
    engine = DualRoutingEngine(merchant_rules, agent_rules, available_psps, False)
    result = engine.resolve()
    
    print("✅ DualRoutingEngine test completed")
    print(f"   Selected PSP: {result['selected_psp']}")
    print(f"   Conflict detected: {result['conflict_detected']}")
    print(f"   Resolution method: {result['resolution_method']}")
    
    if result['conflicts']:
        print("   Conflicts:")
        for conflict in result['conflicts']:
            print(f"   - {conflict['psp']}: merchant={conflict['merchant_rule']}, agent={conflict['agent_rule']}")
    
except Exception as e:
    print(f"❌ DualRoutingEngine test failed: {e}")
EOF
echo

# Step 8: Cleanup
echo "=== Step 8: Cleanup ==="
echo "Deleting test policies..."
api_call DELETE "/employee/routing/policies/merchant/$TEST_MERCHANT_ID" >/dev/null 2>&1
api_call DELETE "/employee/routing/policies/agent/$TEST_AGENT_ID" >/dev/null 2>&1
echo "✅ Cleanup completed"
echo

# Summary
echo "======================================================="
echo "[Phase 4++] TEST SUMMARY"
echo "======================================================="
echo "✅ Key features tested:"
echo "  - Migration 011 execution"
echo "  - Merchant routing policy creation"
echo "  - Agent routing policy creation"
echo "  - Routing conflict detection"
echo "  - Routing simulation"
echo "  - Conflict analytics"
echo "  - DualRoutingEngine logic"
echo
echo "💡 Scenario tested: Merchant excludes Stripe, Agent prefers Stripe"
echo "   Expected: Conflict detected, Adyen selected (merchant rule wins)"
echo
echo "[Phase 4++] Test completed!"
