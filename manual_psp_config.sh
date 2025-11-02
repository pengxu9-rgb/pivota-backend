#!/bin/bash

echo "🔑 手动配置 PSP API Keys"
echo "=========================================="
echo ""
echo "⚠️  请准备好你的 PSP API Keys"
echo ""

# API 配置
API_URL="https://web-production-fedb.up.railway.app"
MERCHANT_ID="merch_208139f7600dbf42"

# 颜色配置
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置 Stripe
echo -e "${YELLOW}配置 Stripe${NC}"
echo "请输入 Stripe Secret Key (sk_test_...):"
read -r STRIPE_KEY
echo "请输入 Stripe Account ID (acct_...):"
read -r STRIPE_ACCOUNT

if [ ! -z "$STRIPE_KEY" ]; then
  curl -X POST "$API_URL/admin/psp/connect" \
    -H "Content-Type: application/json" \
    -d '{
      "merchant_id": "'$MERCHANT_ID'",
      "provider": "stripe",
      "api_key": "'$STRIPE_KEY'",
      "account_id": "'$STRIPE_ACCOUNT'",
      "status": "active"
    }' 2>/dev/null | python3 -m json.tool
  echo -e "${GREEN}✅ Stripe 配置完成${NC}"
else
  echo -e "${RED}跳过 Stripe${NC}"
fi

echo ""

# 配置 Adyen
echo -e "${YELLOW}配置 Adyen${NC}"
echo "请输入 Adyen API Key:"
read -r ADYEN_KEY
echo "请输入 Adyen Merchant Account:"
read -r ADYEN_ACCOUNT

if [ ! -z "$ADYEN_KEY" ]; then
  curl -X POST "$API_URL/admin/psp/connect" \
    -H "Content-Type: application/json" \
    -d '{
      "merchant_id": "'$MERCHANT_ID'",
      "provider": "adyen",
      "api_key": "'$ADYEN_KEY'",
      "account_id": "'$ADYEN_ACCOUNT'",
      "status": "active"
    }' 2>/dev/null | python3 -m json.tool
  echo -e "${GREEN}✅ Adyen 配置完成${NC}"
else
  echo -e "${RED}跳过 Adyen${NC}"
fi

echo ""

# 配置 Checkout
echo -e "${YELLOW}配置 Checkout${NC}"
echo "请输入 Checkout Secret Key (sk_sbox_...):"
read -r CHECKOUT_KEY
echo "请输入 Checkout Processing Channel ID (pc_...):"
read -r CHECKOUT_CHANNEL

if [ ! -z "$CHECKOUT_KEY" ]; then
  curl -X POST "$API_URL/admin/psp/connect" \
    -H "Content-Type: application/json" \
    -d '{
      "merchant_id": "'$MERCHANT_ID'",
      "provider": "checkout",
      "api_key": "'$CHECKOUT_KEY'",
      "account_id": "'$CHECKOUT_CHANNEL'",
      "status": "active"
    }' 2>/dev/null | python3 -m json.tool
  echo -e "${GREEN}✅ Checkout 配置完成${NC}"
else
  echo -e "${RED}跳过 Checkout${NC}"
fi

echo ""

# 配置 PayPal
echo -e "${YELLOW}配置 PayPal${NC}"
echo "请输入 PayPal Client ID:"
read -r PAYPAL_CLIENT
echo "请输入 PayPal Secret:"
read -r PAYPAL_SECRET
echo "请输入 PayPal Account Email:"
read -r PAYPAL_ACCOUNT

if [ ! -z "$PAYPAL_CLIENT" ]; then
  curl -X POST "$API_URL/admin/psp/connect" \
    -H "Content-Type: application/json" \
    -d '{
      "merchant_id": "'$MERCHANT_ID'",
      "provider": "paypal",
      "api_key": "'$PAYPAL_CLIENT'",
      "secret_key": "'$PAYPAL_SECRET'",
      "account_id": "'$PAYPAL_ACCOUNT'",
      "status": "active"
    }' 2>/dev/null | python3 -m json.tool
  echo -e "${GREEN}✅ PayPal 配置完成${NC}"
else
  echo -e "${RED}跳过 PayPal${NC}"
fi

echo ""
echo -e "${GREEN}=========================================="
echo "✅ PSP 配置完成！"
echo "==========================================${NC}"

