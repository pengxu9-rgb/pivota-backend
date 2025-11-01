#!/bin/bash

# Check why merchant_count is 0
TOKEN="$1"

if [ -z "$TOKEN" ]; then
    echo "Usage: $0 TOKEN"
    exit 1
fi

API_URL="https://web-production-fedb.up.railway.app"

echo "======================================"
echo "🔍 Checking agent_merchants table..."
echo "======================================"

# We need to run SQL to check the data
# Let me create a SQL query instead

cat > /tmp/check_merchants.sql << 'EOF'
-- 1. Check agent_merchants table for this agent
SELECT 
    'agent_merchants table' as source,
    COUNT(*) as count
FROM agent_merchants
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 2. Check merchants from orders table (where the real orders are)
SELECT 
    'merchants from orders' as source,
    COUNT(DISTINCT merchant_id) as unique_merchants,
    STRING_AGG(DISTINCT merchant_id, ', ') as merchant_ids
FROM orders
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 3. Show the actual order details
SELECT 
    order_id,
    merchant_id,
    agent_id,
    total,
    payment_status,
    created_at
FROM orders
WHERE agent_id = 'agent_ee38f2b3645a2ec2';
EOF

echo "SQL query created at /tmp/check_merchants.sql"
echo ""
echo "======================================"
echo "📊 Analysis:"
echo "======================================"
echo "The issue is likely one of:"
echo ""
echo "1. agent_merchants table is empty (no relationships created)"
echo "   - Orders have agent_id directly"
echo "   - But agent_merchants table wasn't populated"
echo ""
echo "2. merchant_count calculation should use:"
echo "   - COUNT(DISTINCT merchant_id) FROM orders WHERE agent_id = ?"
echo "   - Instead of JOIN agent_merchants"
echo ""
echo "======================================"
echo "💡 Solution:"
echo "======================================"
echo "We should calculate merchant_count from orders table directly,"
echo "not from agent_merchants relationship table."
echo "======================================"
