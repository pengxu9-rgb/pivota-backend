#!/bin/bash

# Simple script to fix agents data via API call
# Usage: ./test_fix_agents.sh <employee_token>

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# API Base URL
API_URL="https://web-production-fedb.up.railway.app"

# Check if token provided
if [ -z "$1" ]; then
    echo -e "${RED}Error: Please provide your employee token${NC}"
    echo "Usage: $0 <employee_token>"
    echo ""
    echo "To get your token:"
    echo "1. Open Employee Portal"
    echo "2. Open browser DevTools (F12)"
    echo "3. Go to Application/Storage > Local Storage"
    echo "4. Copy the 'employee_token' value"
    exit 1
fi

TOKEN="$1"

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  FIX AGENTS DATA SCRIPT${NC}"
echo -e "${YELLOW}========================================${NC}"
echo ""

# First check status
echo -e "${GREEN}1. Checking current agents status...${NC}"
STATUS_RESPONSE=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "${API_URL}/admin/fix/agents-status")

echo "Current status:"
echo "$STATUS_RESPONSE" | jq '.' || echo "$STATUS_RESPONSE"
echo ""

# Check if any need fixing
NEEDS_FIX=$(echo "$STATUS_RESPONSE" | jq -r '.needs_fix // 0')

if [ "$NEEDS_FIX" = "0" ]; then
    echo -e "${GREEN}✅ All agents already have name and email. Nothing to fix!${NC}"
    exit 0
fi

echo -e "${YELLOW}Found $NEEDS_FIX agents that need fixing${NC}"
echo ""

# Ask for confirmation
read -p "Do you want to fix them now? (yes/no): " -r REPLY
if [[ ! $REPLY =~ ^[Yy]es$ ]]; then
    echo -e "${RED}Operation cancelled${NC}"
    exit 0
fi

# Run the fix
echo ""
echo -e "${GREEN}2. Fixing agents data...${NC}"
FIX_RESPONSE=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" \
    "${API_URL}/admin/fix/agents-data")

echo "Fix result:"
echo "$FIX_RESPONSE" | jq '.' || echo "$FIX_RESPONSE"
echo ""

# Check if successful
STATUS=$(echo "$FIX_RESPONSE" | jq -r '.status // "error"')

if [ "$STATUS" = "success" ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✅ SUCCESS! Agents data has been fixed.${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "Next steps:"
    echo "1. Go to Employee Portal"
    echo "2. Navigate to Agents page"  
    echo "3. Refresh the page"
    echo "4. You should now see agent names!"
else
    echo -e "${RED}❌ Failed to fix agents data${NC}"
    echo "Error response:"
    echo "$FIX_RESPONSE"
fi
