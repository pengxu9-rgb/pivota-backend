#!/bin/bash

# Test Agents Phase 3 - Complete Observability & Governance
# Usage: ./test_phase3_complete.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

echo "======================================"
echo "🧪 Testing Phase 3 - Observability & Governance"
echo "======================================"
echo ""

echo "1️⃣ Trigger Metrics Collection"
echo "--------------------------------------"
curl -sS -X POST "$API_URL/admin/governance/metrics/collect-now" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo ""
echo ""
echo "2️⃣ Get Metrics History"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/metrics-history?hours=1" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
metrics = data.get('metrics', [])
print(f'Total metrics collected: {len(metrics)}')
if metrics:
    latest = metrics[0]
    print(f'Latest metrics:')
    print(f'  - Avg Response Time: {latest.get(\"avg_response_time_ms\")}ms')
    print(f'  - Success Rate: {latest.get(\"success_rate\")}%')
    print(f'  - Error Rate: {latest.get(\"error_rate\")}%')
    print(f'  - Queries/Min: {latest.get(\"queries_per_min\")}')
else:
    print('No metrics data yet (need recent API activity)')
"

echo ""
echo ""
echo "3️⃣ Get Health Score"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/health-score" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Health Score: {data.get(\"health_score\")}/100')
print(f'Grade: {data.get(\"grade\")}')
if data.get('details'):
    print(f'Details: {data.get(\"details\")}')
"

echo ""
echo ""
echo "4️⃣ Get Agent Alerts"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/alerts?resolved=false" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('alerts', [])
print(f'Unresolved alerts: {len(alerts)}')
for alert in alerts:
    print(f'  - [{alert.get(\"severity\")}] {alert.get(\"alert_type\")}: {alert.get(\"message\")}')
"

echo ""
echo ""
echo "5️⃣ Get All Critical Alerts"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/alerts?severity=critical&resolved=false" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('alerts', [])
print(f'Critical alerts across all agents: {len(alerts)}')
for alert in alerts:
    print(f'  - Agent: {alert.get(\"agent_name\")}, Type: {alert.get(\"alert_type\")}')
"

echo ""
echo ""
echo "6️⃣ Get Pending Governance Actions"
echo "--------------------------------------"
curl -sS "$API_URL/admin/governance/pending-actions" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
actions = data.get('actions', [])
print(f'Pending governance actions: {len(actions)}')
for action in actions:
    print(f'  - Agent: {action.get(\"agent_name\")}')
    print(f'    Type: {action.get(\"action_type\")}')
    print(f'    Reason: {action.get(\"reason\")}')
    print(f'    Triggered by: {action.get(\"triggered_by\")}')
"

echo ""
echo ""
echo "======================================"
echo "✅ Phase 3 Backend Testing Complete"
echo "======================================"
echo ""
echo "Summary:"
echo "- Metrics collection: Working (manual trigger)"
echo "- Alerts system: Ready (no alerts if no anomalies)"
echo "- Health score: Calculating based on available data"
echo "- Governance: Workflow ready for approval/rejection"
echo ""
echo "Next steps:"
echo "1. Generate some API activity to create metrics data"
echo "2. Check frontend UI for Alerts and Metrics sections"
echo "3. Set up automatic metrics collection (scheduler)"
echo "======================================"
