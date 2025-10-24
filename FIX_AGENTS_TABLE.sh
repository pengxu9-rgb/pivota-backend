#!/bin/bash

# Fix agents table by dropping and recreating with correct schema
# WARNING: This will delete all existing agents!

echo "🔧 Fixing agents table schema..."
echo ""

curl -X POST "https://web-production-fedb.up.railway.app/admin/fix/agents-table" \
  -H "Content-Type: application/json" \
  | jq .

echo ""
echo "✅ Done! You can now try agent registration again."




