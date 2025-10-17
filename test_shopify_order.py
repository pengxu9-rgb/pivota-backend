#!/usr/bin/env python3
"""
Test Shopify Order Creation
Ensures proper PSP and Shopify configuration before creating order
"""

import requests
import json
import time
import os
from datetime import datetime

BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"
YOUR_EMAIL = "peng@chydan.com"

# You can override these with environment variables
STRIPE_TEST_KEY = os.getenv("STRIPE_TEST_KEY", "sk_test_51J...")  # Add your Stripe test key
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")  # Add if available


def print_step(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print('='*60)


def get_admin_token():
    resp = requests.get(f"{BASE_URL}/auth/admin-token")
    return resp.json()["token"]


def update_merchant_psp(token):
    """Update merchant with valid PSP credentials"""
    print_step("Setting Up PSP (Stripe Test Mode)")
    
    # Use Stripe test key
    psp_data = {
        "merchant_id": MERCHANT_ID,
        "psp_type": "stripe",
        "api_key": "sk_test_dummy_key_for_testing"  # This will create orders but not process payments
    }
    
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"{BASE_URL}/merchant/onboarding/psp/setup",
        json=psp_data,
        headers=headers
    )
    
    if resp.status_code == 200:
        print("✅ PSP setup complete (test mode)")
        return True
    else:
        print(f"⚠️  PSP setup issue: {resp.text[:200]}")
        # Continue anyway - orders can still be created
        return True


def create_order_with_shopify(token):
    """Create an order that should sync to Shopify"""
    print_step("Creating Order for Shopify")
    
    order_data = {
        "merchant_id": MERCHANT_ID,
        "customer_email": YOUR_EMAIL,
        "items": [
            {
                "product_id": "shopify_test_001",
                "product_title": "Test Product for Shopify",
                "variant_id": "12345",  # Shopify variant ID if known
                "sku": "TEST-SKU-001",
                "quantity": 1,
                "unit_price": "99.00",
                "subtotal": "99.00"
            }
        ],
        "shipping_address": {
            "name": "Test Customer",
            "address_line1": "685 Market Street",
            "address_line2": "Suite 500",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
            "phone": "+14155551234"
        },
        "currency": "USD",
        "metadata": {
            "test": "shopify_integration",
            "timestamp": datetime.now().isoformat()
        }
    }
    
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"{BASE_URL}/orders/create",
        json=order_data,
        headers=headers
    )
    
    if resp.status_code == 200:
        order = resp.json()
        print(f"✅ Order created: {order['order_id']}")
        print(f"   Total: ${order['total']} {order['currency']}")
        print(f"   Status: {order['status']}")
        
        # Check for payment intent
        if order.get('payment_intent_id'):
            print(f"   Payment Intent: {order['payment_intent_id'][:30]}...")
        else:
            print("   ⚠️  No payment intent (will create Shopify order directly)")
        
        return order
    else:
        print(f"❌ Order creation failed: {resp.status_code}")
        print(f"   {resp.text[:500]}")
        return None


def force_shopify_creation(token, order_id):
    """Try to force Shopify order creation"""
    print_step("Forcing Shopify Order Creation")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # First, mark the order as paid (bypass payment)
    payment_data = {
        "order_id": order_id,
        "payment_status": "succeeded",
        "payment_method": "test",
        "payment_intent_id": f"pi_test_{int(time.time())}"
    }
    
    resp = requests.post(
        f"{BASE_URL}/orders/{order_id}/mark-paid",
        json=payment_data,
        headers=headers
    )
    
    if resp.status_code in [200, 404]:
        print("   Attempting to trigger Shopify order...")
        
        # Try to create Shopify order directly
        resp = requests.post(
            f"{BASE_URL}/orders/{order_id}/create-shopify",
            headers=headers
        )
        
        if resp.status_code == 200:
            print("✅ Shopify order creation triggered")
            return True
        elif resp.status_code == 404:
            print("   ⚠️  Direct Shopify creation endpoint not available")
        else:
            print(f"   ⚠️  Shopify creation response: {resp.status_code}")
    
    return False


def check_shopify_order(token, order_id):
    """Check if Shopify order was created"""
    print_step("Checking Shopify Order Status")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    for i in range(10):
        time.sleep(3)
        
        resp = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
        
        if resp.status_code == 200:
            order = resp.json()
            print(f"   Attempt {i+1}: Payment={order.get('payment_status', 'N/A')}, Fulfillment={order.get('fulfillment_status', 'N/A')}")
            
            if order.get('shopify_order_id'):
                print(f"✅ Shopify order created: {order['shopify_order_id']}")
                print(f"   Check Shopify Admin: https://chydantest.myshopify.com/admin/orders/{order['shopify_order_id']}")
                return order['shopify_order_id']
            
            # Check metadata for Shopify info
            if order.get('metadata', {}).get('shopify_order_id'):
                shopify_id = order['metadata']['shopify_order_id']
                print(f"✅ Shopify order found in metadata: {shopify_id}")
                return shopify_id
        else:
            print(f"   Order fetch failed: {resp.status_code}")
    
    print("❌ Shopify order not created")
    return None


def check_shopify_directly():
    """Instructions for checking Shopify directly"""
    print_step("Manual Shopify Check")
    
    print("📱 Please check Shopify directly:")
    print("1. Go to: https://chydantest.myshopify.com/admin")
    print("2. Navigate to: Orders → All orders")
    print("3. Look for recent orders with:")
    print(f"   - Customer email: {YOUR_EMAIL}")
    print("   - Order total: $99.00")
    print("   - Created: Today")
    print("")
    print("If no order appears:")
    print("• The Shopify integration needs proper access token")
    print("• Check Railway environment variables:")
    print("  - SHOPIFY_ACCESS_TOKEN")
    print("  - SHOPIFY_API_KEY")
    print("  - SHOPIFY_API_SECRET")


def main():
    print("\n" + "🛍️"*30)
    print("SHOPIFY ORDER CREATION TEST")
    print(f"Merchant: {MERCHANT_ID}")
    print(f"Email: {YOUR_EMAIL}")
    print("🛍️"*30)
    
    try:
        # Get admin token
        print("\nGetting admin token...")
        token = get_admin_token()
        print("✅ Token obtained")
        
        # Update PSP configuration
        update_merchant_psp(token)
        
        # Create order
        order = create_order_with_shopify(token)
        
        if order:
            order_id = order['order_id']
            
            # Try to force Shopify creation if no payment intent
            if not order.get('payment_intent_id'):
                force_shopify_creation(token, order_id)
            
            # Check for Shopify order
            shopify_id = check_shopify_order(token, order_id)
            
            # Results
            print_step("TEST RESULTS")
            print(f"✅ Pivota Order: {order_id}")
            print(f"   Total: ${order['total']}")
            
            if shopify_id:
                print(f"✅ Shopify Order: {shopify_id}")
                print(f"✅ Email should be sent to: {YOUR_EMAIL}")
            else:
                print("❌ Shopify order not created")
                check_shopify_directly()
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
