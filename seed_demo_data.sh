#!/bin/bash

# Seed Demo Data for Phase 3 Metrics and Alerts
# Usage: ./seed_demo_data.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

echo "======================================"
echo "🌱 Seeding Test Data for Phase 3 Demo"
echo "======================================"
echo ""

echo "Creating test metrics and alerts..."
curl -sS -X POST "$API_URL/admin/test-data/seed-metrics-and-alerts?agent_id=$AGENT_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "📊 Verifying Seeded Data"
echo "======================================"

echo ""
echo "1️⃣ Check Metrics History (last 4 hours)"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/metrics-history?hours=4" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
metrics = data.get('metrics', [])
print(f'Total metrics: {len(metrics)}')
if metrics:
    latest = metrics[0]
    print(f'Latest metrics:')
    print(f'  Avg Response Time: {latest.get(\"avg_response_time_ms\")}ms')
    print(f'  Success Rate: {latest.get(\"success_rate\")}%')
    print(f'  Error Rate: {latest.get(\"error_rate\")}%')
    print(f'  Queries/Min: {latest.get(\"queries_per_min\")}')
"

echo ""
echo ""
echo "2️⃣ Check Alerts"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/alerts?resolved=false" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('alerts', [])
print(f'Unresolved alerts: {len(alerts)}')
for alert in alerts:
    print(f'  [{alert.get(\"severity\").upper()}] {alert.get(\"alert_type\")}: {alert.get(\"message\")}')
"

echo ""
echo ""
echo "3️⃣ Check Health Score (should improve with data)"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/health-score" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Health Score: {data.get(\"health_score\")}/100')
print(f'Grade: {data.get(\"grade\")}')
print(f'Details: {data.get(\"details\")}')
"

echo ""
echo ""
echo "======================================"
echo "✅ Demo Data Seeded Successfully!"
echo "======================================"
echo ""
echo "What was created:"
echo "- 48 metrics data points (last 4 hours, 5-min intervals)"
echo "- Includes a performance spike at 2 hours ago"
echo "- 3 test alerts (1 critical, 2 warnings)"
echo ""
echo "Next steps:"
echo "1. Refresh Employee Portal (Cmd+Shift+R)"
echo "2. Click agent 'View' button"
echo "3. Expand 'Alerts & Anomalies' - see 3 alerts"
echo "4. Expand 'Performance Metrics History' - see metrics table"
echo "5. Should see performance spike in the data"
echo ""
echo "To clear demo data:"
echo "  curl -X DELETE '$API_URL/admin/test-data/clear-test-data?agent_id=$AGENT_ID' \\"
echo "    -H 'Authorization: Bearer $TOKEN'"
echo "======================================"

# Seed Demo Data for Phase 3 Metrics and Alerts
# Usage: ./seed_demo_data.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

echo "======================================"
echo "🌱 Seeding Test Data for Phase 3 Demo"
echo "======================================"
echo ""

echo "Creating test metrics and alerts..."
curl -sS -X POST "$API_URL/admin/test-data/seed-metrics-and-alerts?agent_id=$AGENT_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "📊 Verifying Seeded Data"
echo "======================================"

echo ""
echo "1️⃣ Check Metrics History (last 4 hours)"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/metrics-history?hours=4" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
metrics = data.get('metrics', [])
print(f'Total metrics: {len(metrics)}')
if metrics:
    latest = metrics[0]
    print(f'Latest metrics:')
    print(f'  Avg Response Time: {latest.get(\"avg_response_time_ms\")}ms')
    print(f'  Success Rate: {latest.get(\"success_rate\")}%')
    print(f'  Error Rate: {latest.get(\"error_rate\")}%')
    print(f'  Queries/Min: {latest.get(\"queries_per_min\")}')
"

echo ""
echo ""
echo "2️⃣ Check Alerts"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/alerts?resolved=false" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('alerts', [])
print(f'Unresolved alerts: {len(alerts)}')
for alert in alerts:
    print(f'  [{alert.get(\"severity\").upper()}] {alert.get(\"alert_type\")}: {alert.get(\"message\")}')
"

echo ""
echo ""
echo "3️⃣ Check Health Score (should improve with data)"
echo "--------------------------------------"
curl -sS "$API_URL/employee/agents/$AGENT_ID/health-score" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Health Score: {data.get(\"health_score\")}/100')
print(f'Grade: {data.get(\"grade\")}')
print(f'Details: {data.get(\"details\")}')
"

echo ""
echo ""
echo "======================================"
echo "✅ Demo Data Seeded Successfully!"
echo "======================================"
echo ""
echo "What was created:"
echo "- 48 metrics data points (last 4 hours, 5-min intervals)"
echo "- Includes a performance spike at 2 hours ago"
echo "- 3 test alerts (1 critical, 2 warnings)"
echo ""
echo "Next steps:"
echo "1. Refresh Employee Portal (Cmd+Shift+R)"
echo "2. Click agent 'View' button"
echo "3. Expand 'Alerts & Anomalies' - see 3 alerts"
echo "4. Expand 'Performance Metrics History' - see metrics table"
echo "5. Should see performance spike in the data"
echo ""
echo "To clear demo data:"
echo "  curl -X DELETE '$API_URL/admin/test-data/clear-test-data?agent_id=$AGENT_ID' \\"
echo "    -H 'Authorization: Bearer $TOKEN'"
echo "======================================"
