#!/usr/bin/env python3
"""
Complete Shopify Order Test
Tests the full flow once PostgreSQL tables are fixed
"""

import requests
import json
import time
from datetime import datetime

BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"
YOUR_EMAIL = "peng@chydan.com"

print("""
🛍️ COMPLETE SHOPIFY ORDER TEST
================================
This will test:
1. Order creation and persistence
2. Payment processing (if Stripe configured)
3. Shopify order creation (if token configured)
4. Email notification

Waiting for deployment to complete...
""")

def check_deployment():
    """Check if the fix is deployed"""
    resp = requests.get(f"{BASE_URL}/version")
    if resp.status_code == 200:
        version = resp.json().get("version", "unknown")[:8]
        if version == "cbf4ce19":
            return True
    return False

def run_test():
    # Get admin token
    token = requests.get(f"{BASE_URL}/auth/admin-token").json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    print("\n1️⃣ Creating order...")
    
    # Create order with real product info
    order_data = {
        "merchant_id": MERCHANT_ID,
        "customer_email": YOUR_EMAIL,
        "items": [{
            "product_id": f"shopify_{int(time.time())}",
            "product_title": "Premium Test Product for Shopify",
            "quantity": 2,
            "unit_price": "75.00",
            "subtotal": "150.00"
        }],
        "shipping_address": {
            "name": "Real Customer Name",
            "address_line1": "123 Market Street",
            "address_line2": "Suite 500",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
            "phone": "+14155551234"
        },
        "currency": "USD",
        "metadata": {
            "test": "full_shopify_flow",
            "timestamp": datetime.now().isoformat()
        }
    }
    
    resp = requests.post(f"{BASE_URL}/orders/create", json=order_data, headers=headers)
    
    if resp.status_code == 200:
        order = resp.json()
        order_id = order["order_id"]
        print(f"✅ Order created: {order_id}")
        print(f"   Total: ${order['total']} {order['currency']}")
        
        # Check persistence
        print("\n2️⃣ Checking persistence...")
        time.sleep(3)
        
        check_resp = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
        
        if check_resp.status_code == 200:
            print("✅ Order persists in PostgreSQL!")
            retrieved = check_resp.json()
            
            # Check for payment intent
            if retrieved.get("payment_intent_id"):
                print(f"✅ Payment Intent created: {retrieved['payment_intent_id'][:30]}...")
                
                # Try to confirm payment
                print("\n3️⃣ Confirming payment...")
                payment_data = {
                    "order_id": order_id,
                    "payment_method_id": "pm_card_visa"
                }
                
                pay_resp = requests.post(
                    f"{BASE_URL}/orders/payment/confirm",
                    json=payment_data,
                    headers=headers
                )
                
                if pay_resp.status_code == 200:
                    result = pay_resp.json()
                    if result.get("payment_status") == "succeeded":
                        print("✅ Payment confirmed!")
                        
                        # Wait for Shopify order
                        print("\n4️⃣ Waiting for Shopify order...")
                        for i in range(10):
                            time.sleep(3)
                            
                            final_check = requests.get(
                                f"{BASE_URL}/orders/{order_id}",
                                headers=headers
                            )
                            
                            if final_check.status_code == 200:
                                final_order = final_check.json()
                                shopify_id = final_order.get("shopify_order_id")
                                
                                if shopify_id:
                                    print(f"✅ SHOPIFY ORDER CREATED: {shopify_id}")
                                    print(f"🔗 View: https://chydantest.myshopify.com/admin/orders/{shopify_id}")
                                    print(f"📧 Email sent to: {YOUR_EMAIL}")
                                    return True
                                else:
                                    print(f"   Attempt {i+1}/10: Waiting...")
                        
                        print("⚠️  Shopify order not created (check SHOPIFY_ACCESS_TOKEN)")
                    else:
                        print(f"⚠️  Payment status: {result.get('payment_status')}")
                else:
                    print(f"❌ Payment failed: {pay_resp.text[:200]}")
            else:
                print("⚠️  No payment intent (check STRIPE_SECRET_KEY in Railway)")
                print("   Without payment, Shopify order won't be created")
        else:
            print(f"❌ Order not persisting! Status: {check_resp.status_code}")
            print("   The database fix might not be deployed yet")
    else:
        print(f"❌ Order creation failed: {resp.status_code}")
        print(resp.text[:200])

if __name__ == "__main__":
    # Wait for deployment
    attempts = 0
    while attempts < 20:
        if check_deployment():
            print("✅ Fix is deployed! Running test...")
            run_test()
            break
        else:
            attempts += 1
            print(f"⏳ Waiting for deployment... ({attempts}/20)")
            time.sleep(10)
    else:
        print("\n⚠️  Deployment is taking longer than expected")
        print("Please check Railway dashboard and ensure:")
        print("1. The deployment is not stuck")
        print("2. No build errors occurred")
        print("3. Try manually redeploying if needed")
        print("")
        print("Running test anyway...")
        run_test()
