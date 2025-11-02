#!/bin/bash

# Diagnose MCP data issue
TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 ADMIN_TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Checking MCP database tables..."
echo "======================================"
echo ""

curl -sS "$API_URL/admin/debug-mcp/check-tables" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "======================================"
echo "📊 Analysis:"
echo "======================================"
echo "If all counts are 0:"
echo "  → Database tables are empty (this is real, not a bug)"
echo "  → Need to add stores/PSPs through UI or import data"
echo ""
echo "If counts > 0 but MCP shows 0:"
echo "  → Query or frontend issue"
echo "  → Share the output above for further debugging"
echo "======================================"

