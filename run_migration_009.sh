#!/bin/bash

# Run Migration 009 - Agents Phase 3 Observability
# Usage: ./run_migration_009.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Step 1: Check migration 009 status"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-009-status" \
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
echo "🚀 Step 2: Running migration 009..."
echo "======================================"
curl -sS -X POST "$API_URL/admin/migrations/run-009-agents-phase3" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "✅ Step 3: Verify migration"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-009-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "📊 Step 4: Trigger metrics collection"
echo "======================================"
curl -sS -X POST "$API_URL/admin/governance/metrics/collect-now" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "✅ Migration 009 Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Metrics will be collected every 5 minutes automatically"
echo "2. Check /agents/{id}/metrics-history for data"
echo "3. Alerts will be generated for anomalies"
echo "4. Governance actions require admin approval"
echo "======================================"

# Run Migration 009 - Agents Phase 3 Observability
# Usage: ./run_migration_009.sh ADMIN_TOKEN

TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Step 1: Check migration 009 status"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-009-status" \
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
echo "🚀 Step 2: Running migration 009..."
echo "======================================"
curl -sS -X POST "$API_URL/admin/migrations/run-009-agents-phase3" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "✅ Step 3: Verify migration"
echo "======================================"
curl -sS "$API_URL/admin/migrations/check-009-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "📊 Step 4: Trigger metrics collection"
echo "======================================"
curl -sS -X POST "$API_URL/admin/governance/metrics/collect-now" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo ""
echo "======================================"
echo "✅ Migration 009 Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Metrics will be collected every 5 minutes automatically"
echo "2. Check /agents/{id}/metrics-history for data"
echo "3. Alerts will be generated for anomalies"
echo "4. Governance actions require admin approval"
echo "======================================"
