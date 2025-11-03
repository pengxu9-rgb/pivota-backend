#!/usr/bin/env python3
"""
Setup Merchant with Real Credentials
This will properly configure the merchant for Shopify orders
"""

import os
import sys

print("""
=========================================
MERCHANT CREDENTIAL SETUP REQUIRED
=========================================

To receive Shopify orders, you need to:

1. GET STRIPE TEST KEYS:
   - Go to: https://dashboard.stripe.com/test/apikeys
   - Copy the "Secret key" (starts with sk_test_)
   
2. GET SHOPIFY ACCESS TOKEN:
   - Go to: https://chydantest.myshopify.com/admin/settings/apps
   - Create a Private App or use existing
   - Get the Admin API access token (shpat_...)

3. ADD TO RAILWAY:
   Go to Railway dashboard and add these variables:
   
   STRIPE_SECRET_KEY=sk_test_YOUR_KEY_HERE
   SHOPIFY_ACCESS_TOKEN=shpat_YOUR_TOKEN_HERE
   SHOPIFY_API_KEY=YOUR_API_KEY
   SHOPIFY_API_SECRET=YOUR_SECRET
   SHOPIFY_WEBHOOK_SECRET=YOUR_WEBHOOK_SECRET

4. AFTER ADDING VARIABLES:
   - Railway will auto-deploy
   - Wait 2-3 minutes
   - Run: python3 test_shopify_order.py

=========================================

Without these credentials:
❌ Payment intents won't be created
❌ Shopify orders won't be created  
❌ Email confirmations won't be sent

With proper credentials:
✅ Orders will process payments
✅ Shopify orders will be created automatically
✅ Customers will receive email confirmations

""")

# Check if we can get credentials from environment
stripe_key = os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_TEST_SECRET_KEY")
shopify_token = os.getenv("SHOPIFY_ACCESS_TOKEN")

if stripe_key and shopify_token:
    print("✅ Credentials detected in environment!")
    print("   You can now run the tests.")
else:
    if not stripe_key:
        print("⚠️  Missing STRIPE_SECRET_KEY")
    if not shopify_token:
        print("⚠️  Missing SHOPIFY_ACCESS_TOKEN")
    print("\nPlease add the missing credentials to Railway.")
