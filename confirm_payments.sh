#!/bin/bash

echo "💳 通过 Agent API 确认所有支付"
echo "=========================================="

API_URL="https://web-production-fedb.up.railway.app"
AGENT_API_KEY="ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 获取所有待确认的订单（前20个）
echo -e "${YELLOW}获取待确认订单列表...${NC}"

# 使用简单的订单ID列表（我们刚创建的）
ORDERS=(
  "ORD_B462D3CD8EF7859A"
  "ORD_72AF62061EACE49F"
  "ORD_AF0B0A5B2ECE96AC"
  "ORD_F532496683BF8C14"
  "ORD_1F262457DDD50028"
  "ORD_5A90D815F29E72F9"
  "ORD_6D054EAEB69B9F71"
  "ORD_3B756385B40B0A1C"
  "ORD_A9C0B61C3103A705"
  "ORD_A79B272D57E6A527"
  "ORD_47769F809BFC37E4"
  "ORD_49B2A854AFFFE4B3"
  "ORD_5B304593157C699F"
  "ORD_A4670ED882B988B9"
  "ORD_7C8FC667C1835BEA"
  "ORD_7598ECA4FB7383C3"
  "ORD_B2CA3759BE416BF9"
  "ORD_00B695F4EED1D9C9"
  "ORD_C1F6DA7D1EAE564D"
  "ORD_6E151AFCA31C83E7"
)

SUCCESS=0
FAILED=0

for ORDER_ID in "${ORDERS[@]}"; do
  echo -e "${YELLOW}确认订单: $ORDER_ID${NC}"
  
  # 调用支付确认 API
  RESPONSE=$(curl -sS -X POST "$API_URL/agent/v1/orders/$ORDER_ID/confirm-payment" \
    -H "x-api-key: $AGENT_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"payment_intent_id": "confirmed_'"$ORDER_ID"'"}' 2>&1)
  
  if echo "$RESPONSE" | grep -q '"success".*true\|"status".*"success"\|"paid"'; then
    echo -e "${GREEN}  ✅ 支付确认成功${NC}"
    SUCCESS=$((SUCCESS + 1))
  else
    echo -e "${RED}  ❌ 失败: $(echo $RESPONSE | head -c 100)${NC}"
    FAILED=$((FAILED + 1))
  fi
  
  sleep 0.3
done

echo ""
echo "=========================================="
echo -e "${GREEN}完成！${NC}"
echo "  成功: $SUCCESS"
echo "  失败: $FAILED"
echo "=========================================="
echo ""
echo "📊 刷新 PSP Overview 查看更新后的数据！"

