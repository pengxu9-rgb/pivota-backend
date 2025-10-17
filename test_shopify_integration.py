#!/usr/bin/env python3
"""
Test Shopify Integration with New Credentials
"""

import requests
import json
import time
from datetime import datetime

BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"
YOUR_EMAIL = "peng@chydan.com"


def get_admin_token():
    resp = requests.get(f"{BASE_URL}/auth/admin-token")
    return resp.json()["token"]


def test_shopify_products(token):
    """Test if we can fetch products from Shopify"""
    print("\n" + "="*60)
    print("  Testing Shopify Product Sync")
    print("="*60)
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # Try to sync products from Shopify
    resp = requests.post(
        f"{BASE_URL}/products/sync/{MERCHANT_ID}",
        headers=headers
    )
    
    if resp.status_code == 200:
        data = resp.json()
        print(f"✅ Product sync initiated: {data}")
        
        # Wait and then fetch products
        time.sleep(3)
        
        resp = requests.get(
            f"{BASE_URL}/products/{MERCHANT_ID}",
            params={"force_refresh": "true"},
            headers=headers
        )
        
        if resp.status_code == 200:
            products = resp.json().get("products", [])
            print(f"✅ Found {len(products)} products from Shopify")
            
            # Show first 3 products
            for i, product in enumerate(products[:3], 1):
                print(f"\n  Product {i}:")
                print(f"    Title: {product.get('title')}")
                print(f"    ID: {product.get('id')}")
                print(f"    Price: ${product.get('price')}")
                
                # Get variant info for order creation
                for variant in product.get('variants', [])[:1]:
                    print(f"    Variant ID: {variant.get('id')}")
                    print(f"    SKU: {variant.get('sku')}")
                    
            return products
        else:
            print(f"❌ Product fetch failed: {resp.text[:200]}")
    else:
        print(f"❌ Product sync failed: {resp.text[:200]}")
        print("\nThis likely means Shopify credentials are not configured properly.")
    
    return []


def create_order_with_shopify_product(token, products):
    """Create an order using real Shopify product data"""
    print("\n" + "="*60)
    print("  Creating Order with Shopify Product")
    print("="*60)
    
    # Use the first available product
    if products:
        product = products[0]
        variant = product.get('variants', [{}])[0] if product.get('variants') else {}
        
        print(f"\nUsing product: {product.get('title')}")
        print(f"  Shopify Product ID: {product.get('id')}")
        print(f"  Variant ID: {variant.get('id')}")
        
        items = [{
            "product_id": str(product.get('id', 'test_001')),
            "product_title": product.get('title', 'Test Product'),
            "variant_id": str(variant.get('id', '')) if variant.get('id') else None,
            "sku": variant.get('sku', ''),
            "quantity": 1,
            "unit_price": str(product.get('price', 99.00)),
            "subtotal": str(product.get('price', 99.00))
        }]
    else:
        print("No Shopify products found, using test product")
        items = [{
            "product_id": "shopify_test_" + str(int(time.time())),
            "product_title": "Test Product for Shopify Order",
            "quantity": 1,
            "unit_price": "149.00",
            "subtotal": "149.00"
        }]
    
    order_data = {
        "merchant_id": MERCHANT_ID,
        "customer_email": YOUR_EMAIL,
        "items": items,
        "shipping_address": {
            "name": "Test Customer",
            "address_line1": "123 Market Street",
            "address_line2": "Suite 100",
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
        print(f"\n✅ Order created: {order['order_id']}")
        print(f"   Total: ${order['total']} {order['currency']}")
        print(f"   Status: {order['status']}")
        
        if order.get('payment_intent_id'):
            print(f"   ✅ Payment Intent: {order['payment_intent_id'][:30]}...")
            print("   ✅ Stripe is working!")
        else:
            print("   ❌ No payment intent - Stripe credentials may not be loaded")
        
        return order
    else:
        print(f"❌ Order creation failed: {resp.status_code}")
        print(f"   {resp.text[:500]}")
        return None


def check_for_shopify_order(token, order_id):
    """Check if Shopify order was created"""
    print("\n" + "="*60)
    print("  Checking for Shopify Order Creation")
    print("="*60)
    
    headers = {"Authorization": f"Bearer {token}"}
    
    print("\nWaiting for Shopify order...")
    for i in range(10):
        time.sleep(3)
        
        # Try to get order status
        resp = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
        
        if resp.status_code == 200:
            order = resp.json()
            
            shopify_id = order.get('shopify_order_id') or order.get('metadata', {}).get('shopify_order_id')
            
            print(f"  Attempt {i+1}: Payment={order.get('payment_status')}, Shopify={shopify_id or 'None'}")
            
            if shopify_id:
                print(f"\n✅ SHOPIFY ORDER CREATED: {shopify_id}")
                print(f"   View in Shopify: https://chydantest.myshopify.com/admin/orders/{shopify_id}")
                print(f"   📧 Email confirmation sent to: {YOUR_EMAIL}")
                return shopify_id
        else:
            print(f"  Attempt {i+1}: Order not found (may still be processing)")
    
    print("\n❌ Shopify order not created after 30 seconds")
    return None


def test_shopify_connection(token):
    """Test if Shopify connection is working"""
    print("\n" + "="*60)
    print("  Testing Shopify Connection Status")
    print("="*60)
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # Check merchant status
    resp = requests.get(
        f"{BASE_URL}/merchant/onboarding/all",
        headers=headers
    )
    
    if resp.status_code == 200:
        merchants = resp.json().get("merchants", [])
        for merchant in merchants:
            if merchant["merchant_id"] == MERCHANT_ID:
                print(f"\nMerchant: {merchant['business_name']}")
                print(f"  PSP Connected: {merchant.get('psp_connected')} (Type: {merchant.get('psp_type')})")
                print(f"  MCP Connected: {merchant.get('mcp_connected')}")
                print(f"  Store URL: {merchant.get('store_url')}")
                
                if not merchant.get('psp_connected'):
                    print("\n❌ PSP not connected - payments won't work")
                if not merchant.get('mcp_connected'):
                    print("❌ MCP not connected - Shopify orders won't be created")
                
                return merchant
    
    return None


def main():
    print("\n" + "🛍️"*35)
    print("COMPLETE SHOPIFY INTEGRATION TEST")
    print(f"Testing with credentials you added to Railway")
    print("🛍️"*35)
    
    try:
        # Get admin token
        print("\nGetting admin token...")
        token = get_admin_token()
        print("✅ Token obtained")
        
        # Test Shopify connection status
        merchant = test_shopify_connection(token)
        
        # Test product sync
        products = test_shopify_products(token)
        
        # Create order
        order = create_order_with_shopify_product(token, products)
        
        if order:
            order_id = order['order_id']
            
            # If we have payment intent, try to confirm payment
            if order.get('payment_intent_id'):
                print("\n" + "="*60)
                print("  Confirming Payment with Stripe")
                print("="*60)
                
                payment_data = {
                    "order_id": order_id,
                    "payment_method_id": "pm_card_visa"
                }
                
                resp = requests.post(
                    f"{BASE_URL}/orders/payment/confirm",
                    json=payment_data,
                    headers={"Authorization": f"Bearer {token}"}
                )
                
                if resp.status_code == 200:
                    result = resp.json()
                    print(f"✅ Payment status: {result.get('payment_status')}")
                else:
                    print(f"❌ Payment failed: {resp.text[:200]}")
            
            # Check for Shopify order
            shopify_id = check_for_shopify_order(token, order_id)
            
            # Final results
            print("\n" + "="*60)
            print("  FINAL RESULTS")
            print("="*60)
            
            print(f"\n✅ Pivota Order: {order_id}")
            print(f"   Total: ${order['total']}")
            
            if order.get('payment_intent_id'):
                print("✅ Stripe Integration: Working")
            else:
                print("❌ Stripe Integration: Not working (check STRIPE_SECRET_KEY in Railway)")
            
            if shopify_id:
                print(f"✅ Shopify Order: {shopify_id}")
                print(f"✅ Email: Sent to {YOUR_EMAIL}")
            else:
                print("❌ Shopify Order: Not created (check SHOPIFY_ACCESS_TOKEN in Railway)")
            
            if not order.get('payment_intent_id') and not shopify_id:
                print("\n⚠️  It appears the credentials haven't been loaded yet.")
                print("   Railway may need to redeploy. Try:")
                print("   1. Make a small code change and push to trigger deployment")
                print("   2. Or manually trigger a redeploy in Railway dashboard")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
