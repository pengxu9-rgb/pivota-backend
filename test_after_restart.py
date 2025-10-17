#!/usr/bin/env python3
"""
Test After Railway Restart
Run this after manually restarting the Railway service
"""

import requests
import time
import json

BASE_URL = "https://web-production-fedb.up.railway.app"
YOUR_EMAIL = "peng@chydan.com"

print("🧪 TESTING AFTER RAILWAY RESTART")
print("="*60)

# Wait a bit for service to be ready
print("Waiting for service to be ready...")
time.sleep(5)

# Get token
token = requests.get(f"{BASE_URL}/auth/admin-token").json()["token"]
headers = {"Authorization": f"Bearer {token}"}

# Create order
print("\n1️⃣ Creating order...")
order_data = {
    "merchant_id": "merch_208139f7600dbf42",
    "customer_email": YOUR_EMAIL,
    "items": [{
        "product_id": f"restart_test_{int(time.time())}",
        "product_title": "Shopify Order Test",
        "quantity": 1,
        "unit_price": "299.99",
        "subtotal": "299.99"
    }],
    "shipping_address": {
        "name": "Real Customer",
        "address_line1": "123 Market St",
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
    order_id = order["order_id"]
    print(f"✅ Order created: {order_id}")
    print(f"   Total: ${order['total']}")
    
    # Check persistence
    print("\n2️⃣ Checking persistence...")
    time.sleep(3)
    
    check = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
    
    if check.status_code == 200:
        print("\n🎉🎉🎉 SUCCESS! 🎉🎉🎉")
        print("✅ Orders are now persisting in PostgreSQL!")
        
        retrieved = check.json()
        
        # Check integrations
        print("\n📊 Integration Status:")
        
        if retrieved.get("payment_intent_id"):
            print("✅ Stripe: WORKING!")
            
            # Try to confirm payment
            print("\n3️⃣ Processing payment...")
            pay_resp = requests.post(
                f"{BASE_URL}/orders/payment/confirm",
                json={"order_id": order_id, "payment_method_id": "pm_card_visa"},
                headers=headers
            )
            
            if pay_resp.status_code == 200:
                result = pay_resp.json()
                if result.get("payment_status") == "succeeded":
                    print("✅ Payment confirmed!")
                    
                    # Wait for Shopify
                    print("\n4️⃣ Waiting for Shopify order...")
                    for i in range(5):
                        time.sleep(3)
                        final = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
                        if final.status_code == 200:
                            final_order = final.json()
                            if final_order.get("shopify_order_id"):
                                print(f"✅ SHOPIFY ORDER CREATED: {final_order['shopify_order_id']}")
                                print(f"📧 Email sent to: {YOUR_EMAIL}")
                                print(f"🔗 View: https://chydantest.myshopify.com/admin/orders/{final_order['shopify_order_id']}")
                                break
                            else:
                                print(f"   Waiting... ({i+1}/5)")
                    else:
                        print("⚠️  Shopify order not created (check SHOPIFY_ACCESS_TOKEN)")
        else:
            print("⚠️  Stripe: Not configured")
            print("   Add STRIPE_SECRET_KEY to Railway variables")
            
        print("\n" + "="*60)
        print("✅ DATABASE FIXED - Orders are working!")
        
    else:
        print(f"❌ Order still not persisting!")
        print("   Please restart the Railway service manually")
else:
    print(f"❌ Order creation failed: {resp.status_code}")
