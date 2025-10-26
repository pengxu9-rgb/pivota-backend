#!/usr/bin/env python3
"""
Generate 10 real orders to test all PSPs end-to-end
"""
import requests
import json
import time
from datetime import datetime

API_BASE = "https://web-production-fedb.up.railway.app"
AGENT_API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
MERCHANT_ID = "merch_208139f7600dbf42"  # chydantest

# Product catalog from real stores
PRODUCTS = [
    {"id": "prod_001", "title": "Premium Headphones", "price": 99.99},
    {"id": "prod_002", "title": "Wireless Mouse", "price": 29.99},
    {"id": "prod_003", "title": "USB-C Cable", "price": 12.99},
    {"id": "prod_004", "title": "Phone Case", "price": 19.99},
    {"id": "prod_005", "title": "Screen Protector", "price": 9.99},
]

# PSP rotation - 10 orders across 4 PSPs
PSP_ROTATION = ["stripe", "checkout", "paypal", "adyen"] * 3  # 12 orders, use first 10

def create_order(order_num, psp, product):
    """Create a single order"""
    
    payload = {
        "merchant_id": MERCHANT_ID,
        "preferred_psp": psp,
        "items": [{
            "product_id": product["id"],
            "product_title": product["title"],
            "quantity": 1,
            "unit_price": product["price"],
            "subtotal": product["price"]
        }],
        "customer_email": f"customer{order_num}@test.com",
        "shipping_address": {
            "name": f"Test Customer {order_num}",
            "address_line1": f"{order_num} Test Street",
            "city": "New York",
            "state": "NY",
            "postal_code": "10001",
            "country": "US"
        }
    }
    
    try:
        response = requests.post(
            f"{API_BASE}/agent/v1/orders/create",
            headers={
                "x-api-key": AGENT_API_KEY,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=15
        )
        
        data = response.json()
        
        # Extract payment info
        payment = data.get("payment", {})
        has_intent = payment.get("payment_intent_id") is not None
        has_secret = payment.get("client_secret") is not None
        
        return {
            "order_num": order_num,
            "psp": psp,
            "product": product["title"],
            "price": product["price"],
            "order_id": data.get("order_id"),
            "status": data.get("status"),
            "payment_intent_id": payment.get("payment_intent_id"),
            "has_client_secret": has_secret,
            "success": has_intent and has_secret
        }
        
    except Exception as e:
        return {
            "order_num": order_num,
            "psp": psp,
            "product": product["title"],
            "price": product["price"],
            "error": str(e),
            "success": False
        }

def main():
    print("=" * 80)
    print("🚀 GENERATING 10 REAL ORDERS - FULL PSP TEST")
    print("=" * 80)
    print()
    print(f"Merchant: {MERCHANT_ID} (chydantest)")
    print(f"Testing: Stripe, Checkout, PayPal, Adyen")
    print()
    print("Starting in 3 seconds...")
    time.sleep(3)
    print()
    
    results = []
    
    for i in range(10):
        order_num = i + 1
        psp = PSP_ROTATION[i]
        product = PRODUCTS[i % len(PRODUCTS)]
        
        print(f"📦 Order {order_num}/10: {product['title']} (${product['price']}) via {psp.upper()}")
        
        result = create_order(order_num, psp, product)
        results.append(result)
        
        if result.get("success"):
            print(f"   ✅ Created: {result['order_id']}")
            print(f"   ✅ Payment Intent: {result['payment_intent_id']}")
            print(f"   ✅ Client Secret: {'YES' if result['has_client_secret'] else 'NO'}")
        else:
            print(f"   ❌ Failed: {result.get('error', 'No payment intent created')}")
        
        print()
        time.sleep(0.5)  # Brief delay between orders
    
    # Summary
    print("=" * 80)
    print("📊 TEST SUMMARY")
    print("=" * 80)
    print()
    
    by_psp = {}
    for r in results:
        psp = r.get("psp", "unknown")
        if psp not in by_psp:
            by_psp[psp] = {"total": 0, "success": 0, "failed": 0}
        by_psp[psp]["total"] += 1
        if r.get("success"):
            by_psp[psp]["success"] += 1
        else:
            by_psp[psp]["failed"] += 1
    
    for psp, stats in sorted(by_psp.items()):
        success_rate = (stats["success"] / stats["total"] * 100) if stats["total"] > 0 else 0
        status = "✅" if stats["success"] == stats["total"] else "⚠️" if stats["success"] > 0 else "❌"
        print(f"{status} {psp.upper():10} | {stats['success']}/{stats['total']} success ({success_rate:.0f}%)")
    
    print()
    
    total_success = sum(r.get("success", False) for r in results)
    total_orders = len(results)
    overall_rate = (total_success / total_orders * 100) if total_orders > 0 else 0
    
    print(f"Overall: {total_success}/{total_orders} orders successful ({overall_rate:.0f}%)")
    print()
    
    # Show failed orders
    failed = [r for r in results if not r.get("success")]
    if failed:
        print("❌ Failed Orders:")
        for r in failed:
            print(f"   Order {r['order_num']}: {r['psp']} - {r.get('error', 'No payment intent')}")
        print()
    else:
        print("🎉 ALL ORDERS SUCCESSFUL!")
        print()
    
    # Export results
    with open("psp_test_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print("Results saved to: psp_test_results.json")
    print()

if __name__ == "__main__":
    main()

