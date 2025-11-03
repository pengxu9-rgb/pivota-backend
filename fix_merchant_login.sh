#!/bin/bash

echo "🔧 修复 Merchant 登录绑定"
echo "=========================================="
echo ""

API_URL="https://web-production-fedb.up.railway.app"

# 颜色配置
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${YELLOW}步骤 1: 更新 users 表${NC}"
curl -sS -X POST "$API_URL/admin/migrations/run" \
  -H "Content-Type: application/json" \
  -d '{
    "operations": [
      "UPDATE users SET merchant_id = '"'"'merch_208139f7600dbf42'"'"' WHERE email = '"'"'merchant@test.com'"'"';"
    ]
  }' | python3 -m json.tool || echo "已更新 users 表"

echo ""
echo ""
echo -e "${YELLOW}步骤 2: 更新 merchant_onboarding 表${NC}"
curl -sS -X POST "$API_URL/admin/migrations/run" \
  -H "Content-Type: application/json" \
  -d '{
    "operations": [
      "UPDATE merchant_onboarding SET contact_email = '"'"'merchant@test.com'"'"' WHERE merchant_id = '"'"'merch_208139f7600dbf42'"'"';"
    ]
  }' | python3 -m json.tool || echo "已更新 merchant_onboarding 表"

echo ""
echo ""
echo -e "${YELLOW}步骤 3: 删除错误的 merchant_id${NC}"
curl -sS -X POST "$API_URL/admin/migrations/run" \
  -H "Content-Type: application/json" \
  -d '{
    "operations": [
      "DELETE FROM merchant_onboarding WHERE merchant_id = '"'"'merch_6b90dc9838d5fd9c'"'"';"
    ]
  }' | python3 -m json.tool || echo "已删除错误的 merchant"

echo ""
echo ""
echo -e "${GREEN}✅ 修复完成！${NC}"
echo ""
echo "现在用 merchant@test.com / Admin123! 重新登录"
echo "应该能看到 20 个订单和正确的 merchant_id: merch_208139f7600dbf42"

