#!/usr/bin/env python3
"""
Test After Adding New Shopify Token
Run this after adding the new credentials to Railway
"""

import requests
import json
import time
from datetime import datetime

BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"
YOUR_EMAIL = "peng@chydan.com"

print("""
🔍 QUICK CREDENTIAL CHECK
=========================
This will test if your new Shopify token is working.
Make sure you've added to Railway:
- SHOPIFY_ACCESS_TOKEN=shpat_xxxxx
- SHOPIFY_SHOP_DOMAIN=chydantest.myshopify.com
- STRIPE_SECRET_KEY=sk_test_xxxxx

Press Enter when Railway has redeployed...
""")
input()

def test():
    # Get token
    token = requests.get(f"{BASE_URL}/auth/admin-token").json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    print("\n1️⃣ Creating test order...")
    
    # Create order
    order_data = {
        "merchant_id": MERCHANT_ID,
        "customer_email": YOUR_EMAIL,
        "items": [{
            "product_id": f"test_{int(time.time())}",
            "product_title": "Shopify Integration Test Product",
            "quantity": 1,
            "unit_price": "99.99",
            "subtotal": "99.99"
        }],
        "shipping_address": {
            "name": "Test Customer",
            "address_line1": "123 Test St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US"
        },
        "currency": "USD"
    }
    
    resp = requests.post(f"{BASE_URL}/orders/create", json=order_data, headers=headers)
    
    if resp.status_code == 200:
        order = resp.json()
        print(f"✅ Order created: {order['order_id']}")
        
        # Check for payment intent
        if order.get('payment_intent_id'):
            print(f"✅ Stripe working! Payment intent: {order['payment_intent_id'][:20]}...")
            
            # Confirm payment
            print("\n2️⃣ Processing payment...")
            payment_resp = requests.post(
                f"{BASE_URL}/orders/payment/confirm",
                json={
                    "order_id": order['order_id'],
                    "payment_method_id": "pm_card_visa"
                },
                headers=headers
            )
            
            if payment_resp.status_code == 200:
                print("✅ Payment confirmed!")
        else:
            print("⚠️  No payment intent - Stripe key may not be loaded")
        
        # Check for Shopify order
        print("\n3️⃣ Waiting for Shopify order...")
        for i in range(10):
            time.sleep(3)
            try:
                order_check = requests.get(
                    f"{BASE_URL}/orders/{order['order_id']}", 
                    headers=headers
                ).json()
                
                if order_check.get('shopify_order_id'):
                    print(f"\n🎉 SUCCESS! Shopify order created: {order_check['shopify_order_id']}")
                    print(f"📧 Email sent to: {YOUR_EMAIL}")
                    print(f"🛍️ View in Shopify: https://chydantest.myshopify.com/admin/orders/{order_check['shopify_order_id']}")
                    return
            except:
                pass
            print(f"   Checking... ({i+1}/10)")
        
        print("\n❌ Shopify order not created - check if token is correct")
    else:
        print(f"❌ Order creation failed: {resp.text[:200]}")

if __name__ == "__main__":
    try:
        test()
    except Exception as e:
        print(f"❌ Error: {e}")
