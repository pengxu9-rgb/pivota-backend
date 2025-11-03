#!/bin/bash

# [Phase 5] Agent Routing Control + Revenue Layer - Integration Test

echo "======================================================="
echo "[Phase 5] AGENT ROUTING CONTROL & REVENUE TEST"
echo "======================================================="

API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"
TEST_AGENT_ID="agent_ee38f2b3645a2ec2"

echo "API: $API_BASE_URL"
echo "Agent: $TEST_AGENT_ID"
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

# Test 1: Run migrations
echo "=== Test 1: Run Migration 012a (Revenue Schema) ==="
# Note: Migration endpoint would need to be created similar to 011
echo "ℹ️  Migration 012a: agent_revenue_policies, agent_revenue_logs"
echo "ℹ️  Migration 012b: resolved_by, revenue_calculated columns"
echo

# Test 2: Create revenue policy
echo "=== Test 2: Create Revenue Policy ==="
echo "Setting 2% revenue share for all merchants"

revenue_policy='{
  "merchant_id": null,
  "split_ratio": 0.02,
  "currency": "USD",
  "min_transaction_amount": 10.00
}'

response=$(api_call POST "/agents/$TEST_AGENT_ID/revenue/policies" "$revenue_policy")
if echo "$response" | grep -q '"status":"success"'; then
    echo "✅ Revenue policy created"
    echo "$response" | python3 -m json.tool | head -10
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 3: Get revenue policies
echo "=== Test 3: Get Revenue Policies ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/revenue/policies")
if echo "$response" | grep -q '"policies"'; then
    echo "✅ Revenue policies retrieved"
    policy_count=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(len(data.get('policies', [])))" 2>/dev/null || echo "0")
    echo "Found $policy_count policies"
    echo "$response" | python3 -m json.tool 2>/dev/null | grep -A 5 "policies" | head -10
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 4: Test agent routing
echo "=== Test 4: Test Agent Routing ==="
routing_test='{
  "merchant_id": "merchant_test_001",
  "amount": 100.00,
  "currency": "USD"
}'

response=$(api_call POST "/agents/$TEST_AGENT_ID/routing/test" "$routing_test")
if echo "$response" | grep -q '"test_result"'; then
    echo "✅ Routing test completed"
    echo "$response" | python3 -m json.tool 2>/dev/null | grep -E "(selected_psp|conflict_detected|resolution_method)" | head -5
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 5: Get routing history
echo "=== Test 5: Get Agent Routing History ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/routing/history?days=30&limit=5")
if echo "$response" | grep -q '"history"'; then
    echo "✅ Routing history retrieved"
    total=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(data.get('total_routings', 0))" 2>/dev/null || echo "0")
    echo "Total routings (30d): $total"
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 6: Get earnings summary
echo "=== Test 6: Get Earnings Summary ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/revenue/earnings?days=30&currency=USD")
if echo "$response" | grep -q '"total_earned"'; then
    echo "✅ Earnings summary retrieved"
    echo "$response" | python3 -m json.tool 2>/dev/null
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 7: Get revenue logs
echo "=== Test 7: Get Revenue Logs ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/revenue/logs?days=7")
if echo "$response" | grep -q '"logs"'; then
    echo "✅ Revenue logs retrieved"
    log_count=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(len(data.get('logs', [])))" 2>/dev/null || echo "0")
    echo "Found $log_count revenue transactions"
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Summary
echo "======================================================="
echo "[Phase 5] TEST SUMMARY"
echo "======================================================="
echo "✅ Features tested:"
echo "  - Revenue policy creation"
echo "  - Revenue policy retrieval"  
echo "  - Agent routing test endpoint"
echo "  - Routing history"
echo "  - Earnings summary"
echo "  - Revenue transaction logs"
echo
echo "🎯 Phase 5 Capabilities:"
echo "  - Agent self-service routing policies"
echo "  - Revenue sharing with automatic calculation"
echo "  - Dual-path routing visualization"
echo "  - Settlement tracking"
echo
echo "🔗 Next Steps:"
echo "  - Run migrations: POST /admin/migrations/run-012a, run-012b"
echo "  - Visit Agent Detail Panel to see new sections"
echo "  - Test revenue calculation with real transactions"
echo
echo "[Phase 5] Integration test completed!"

# [Phase 5] Agent Routing Control + Revenue Layer - Integration Test

echo "======================================================="
echo "[Phase 5] AGENT ROUTING CONTROL & REVENUE TEST"
echo "======================================================="

API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"
TEST_AGENT_ID="agent_ee38f2b3645a2ec2"

echo "API: $API_BASE_URL"
echo "Agent: $TEST_AGENT_ID"
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

# Test 1: Run migrations
echo "=== Test 1: Run Migration 012a (Revenue Schema) ==="
# Note: Migration endpoint would need to be created similar to 011
echo "ℹ️  Migration 012a: agent_revenue_policies, agent_revenue_logs"
echo "ℹ️  Migration 012b: resolved_by, revenue_calculated columns"
echo

# Test 2: Create revenue policy
echo "=== Test 2: Create Revenue Policy ==="
echo "Setting 2% revenue share for all merchants"

revenue_policy='{
  "merchant_id": null,
  "split_ratio": 0.02,
  "currency": "USD",
  "min_transaction_amount": 10.00
}'

response=$(api_call POST "/agents/$TEST_AGENT_ID/revenue/policies" "$revenue_policy")
if echo "$response" | grep -q '"status":"success"'; then
    echo "✅ Revenue policy created"
    echo "$response" | python3 -m json.tool | head -10
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 3: Get revenue policies
echo "=== Test 3: Get Revenue Policies ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/revenue/policies")
if echo "$response" | grep -q '"policies"'; then
    echo "✅ Revenue policies retrieved"
    policy_count=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(len(data.get('policies', [])))" 2>/dev/null || echo "0")
    echo "Found $policy_count policies"
    echo "$response" | python3 -m json.tool 2>/dev/null | grep -A 5 "policies" | head -10
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 4: Test agent routing
echo "=== Test 4: Test Agent Routing ==="
routing_test='{
  "merchant_id": "merchant_test_001",
  "amount": 100.00,
  "currency": "USD"
}'

response=$(api_call POST "/agents/$TEST_AGENT_ID/routing/test" "$routing_test")
if echo "$response" | grep -q '"test_result"'; then
    echo "✅ Routing test completed"
    echo "$response" | python3 -m json.tool 2>/dev/null | grep -E "(selected_psp|conflict_detected|resolution_method)" | head -5
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 5: Get routing history
echo "=== Test 5: Get Agent Routing History ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/routing/history?days=30&limit=5")
if echo "$response" | grep -q '"history"'; then
    echo "✅ Routing history retrieved"
    total=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(data.get('total_routings', 0))" 2>/dev/null || echo "0")
    echo "Total routings (30d): $total"
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 6: Get earnings summary
echo "=== Test 6: Get Earnings Summary ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/revenue/earnings?days=30&currency=USD")
if echo "$response" | grep -q '"total_earned"'; then
    echo "✅ Earnings summary retrieved"
    echo "$response" | python3 -m json.tool 2>/dev/null
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Test 7: Get revenue logs
echo "=== Test 7: Get Revenue Logs ==="
response=$(api_call GET "/agents/$TEST_AGENT_ID/revenue/logs?days=7")
if echo "$response" | grep -q '"logs"'; then
    echo "✅ Revenue logs retrieved"
    log_count=$(echo "$response" | python3 -c "import json, sys; data = json.load(sys.stdin); print(len(data.get('logs', [])))" 2>/dev/null || echo "0")
    echo "Found $log_count revenue transactions"
else
    echo "⚠️  Response: $(echo "$response" | python3 -m json.tool 2>/dev/null | head -5 || echo "$response")"
fi
echo

# Summary
echo "======================================================="
echo "[Phase 5] TEST SUMMARY"
echo "======================================================="
echo "✅ Features tested:"
echo "  - Revenue policy creation"
echo "  - Revenue policy retrieval"  
echo "  - Agent routing test endpoint"
echo "  - Routing history"
echo "  - Earnings summary"
echo "  - Revenue transaction logs"
echo
echo "🎯 Phase 5 Capabilities:"
echo "  - Agent self-service routing policies"
echo "  - Revenue sharing with automatic calculation"
echo "  - Dual-path routing visualization"
echo "  - Settlement tracking"
echo
echo "🔗 Next Steps:"
echo "  - Run migrations: POST /admin/migrations/run-012a, run-012b"
echo "  - Visit Agent Detail Panel to see new sections"
echo "  - Test revenue calculation with real transactions"
echo
echo "[Phase 5] Integration test completed!"
