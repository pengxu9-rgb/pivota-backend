#!/usr/bin/env python3
"""诊断 PSP 配置"""

import requests
import json

BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"

print("🔍 Diagnosing PSP Configuration\n")

# 检查 Checkout PSP 是否在数据库中
print("=" * 60)
print("Checking PSP configuration for merchant:", MERCHANT_ID)
print("=" * 60)

# 由于我们无法直接查询数据库，让我们通过创建订单来观察日志
print("\n📝 Creating test order with Checkout...")
print("Please check Railway logs for:")
print("1. 'Found checkout key in DB'")
print("2. 'account_id' or 'public_key' value")
print("3. Checkout API response")
print("4. Any error messages")

print("\n💡 Expected in logs:")
print("- INFO - Found checkout key in DB for merchant merch_208139f7600dbf42")
print("- 🔍 Checkout: Creating payment intent...")
print("- API Key: sk_sbox_...")
print("- Response: XXX")
print("- If successful: payment_intent_id should not be null")

print("\n⚠️  Common issues:")
print("1. account_id is NULL in database → processing_channel_id_required error")
print("2. API key invalid → 401 error")
print("3. Payload incorrect → 422 error")
print("4. Code exception → no API call at all")

