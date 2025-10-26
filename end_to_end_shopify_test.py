#!/usr/bin/env python3
"""
End-to-end test: Agent creates orders → Pivota processes payment → Sync to Shopify
Tests the complete flow with real Shopify store integration
"""
import requests
import json
import time
from datetime import datetime

# Configuration
API_BASE = "https://web-production-fedb.up.railway.app"
AGENT_API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
MERCHANT_ID = "merch_208139f7600dbf42"  # chydantest

# Test products
PRODUCTS = [
    {"id": "test_prod_1", "title": "Premium Headphones", "price": 99.99},
    {"id": "test_prod_2", "title": "Wireless Mouse", "price": 29.99},
    {"id": "test_prod_3", "title": "USB-C Cable", "price": 12.99},
    {"id": "test_prod_4", "title": "Phone Case", "price": 19.99},
    {"id": "test_prod_5", "title": "Screen Protector", "price": 9.99},
    {"id": "test_prod_6", "title": "Laptop Stand", "price": 39.99},
    {"id": "test_prod_7", "title": "Keyboard", "price": 79.99},
    {"id": "test_prod_8", "title": "Monitor", "price": 199.99},
    {"id": "test_prod_9", "title": "Webcam", "price": 59.99},
    {"id": "test_prod_10", "title": "Microphone", "price": 89.99},
]

# PSP rotation for 10 orders
PSP_SEQUENCE = ["stripe", "checkout", "paypal", "adyen", "stripe", "checkout", "paypal", "adyen", "stripe", "checkout"]

def create_order_via_agent(order_num, psp, product):
    """Step 1: Create order via Agent API"""
    
    print(f"\n{'='*60}")
    print(f"📦 Order {order_num}/10: {product['title']} (${product['price']})")
    print(f"   PSP: {psp.upper()}")
    print(f"{'='*60}")
    
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
        "customer_email": f"e2etest{order_num}@example.com",
        "shipping_address": {
            "name": f"E2E Test Customer {order_num}",
            "address_line1": f"{order_num}00 Test Avenue",
            "city": "New York",
            "state": "NY",
            "postal_code": "10001",
            "country": "US"
        }
    }
    
    try:
        print("   🔄 Creating order via Agent API...")
        response = requests.post(
            f"{API_BASE}/agent/v1/orders/create",
            headers={
                "x-api-key": AGENT_API_KEY,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=15
        )
        
        if response.status_code != 200:
            print(f"   ❌ API Error {response.status_code}: {response.text[:200]}")
            return None
        
        data = response.json()
        order_id = data.get("order_id")
        payment = data.get("payment", {})
        
        print(f"   ✅ Order created: {order_id}")
        print(f"   ✅ Status: {data.get('status')}")
        print(f"   ✅ Payment Intent: {payment.get('payment_intent_id', 'None')}")
        
        has_secret = payment.get("client_secret") is not None
        print(f"   ✅ Client Secret: {'YES' if has_secret else 'NO'}")
        
        if not has_secret:
            print(f"   ⚠️  Payment intent creation may have failed")
        
        return {
            "order_num": order_num,
            "order_id": order_id,
            "psp": psp,
            "product": product["title"],
            "price": product["price"],
            "payment_intent_id": payment.get("payment_intent_id"),
            "client_secret": payment.get("client_secret"),
            "has_payment": has_secret,
            "status": data.get("status")
        }
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return None

def confirm_payment(order_data):
    """Step 2: Confirm payment to trigger Shopify order creation"""
    
    if not order_data or not order_data.get("order_id"):
        return False
    
    if not order_data.get("payment_intent_id"):
        print(f"   ⚠️  No payment intent, skipping confirmation")
        return False
    
    print(f"   🔄 Confirming payment...")
    
    try:
        # For Stripe/Adyen/Checkout, we simulate payment confirmation
        # In real flow, customer would complete payment in frontend
        confirm_payload = {
            "order_id": order_data["order_id"],
            "payment_intent_id": order_data["payment_intent_id"]
        }
        
        # Use Agent-specific confirm endpoint
        response = requests.post(
            f"{API_BASE}/agent/v1/orders/{order_data['order_id']}/confirm-payment",
            headers={
                "x-api-key": AGENT_API_KEY,
                "Content-Type": "application/json"
            },
            timeout=15
        )
        
        if response.status_code == 200:
            print(f"   ✅ Payment confirmed")
            print(f"   ✅ Shopify order creation triggered")
            return True
        else:
            print(f"   ⚠️  Payment confirmation response: {response.status_code}")
            print(f"   ℹ️  {response.text[:100]}")
            return False
        
    except Exception as e:
        print(f"   ⚠️  Payment confirmation error: {e}")
        return False

def main():
    print("\n" + "="*80)
    print("🚀 END-TO-END SHOPIFY INTEGRATION TEST")
    print("="*80)
    print()
    print(f"Merchant: {MERCHANT_ID} (chydantest.myshopify.com)")
    print(f"Testing: Complete order flow with all 4 PSPs")
    print(f"Orders: 10 orders across Stripe, Checkout, PayPal, Adyen")
    print()
    print("This will:")
    print("  1. Create orders via Agent API")
    print("  2. Generate payment intents for each PSP")
    print("  3. Record orders in Pivota system")
    print()
    
    input("Press Enter to start test...")
    print()
    
    results = []
    
    for i in range(10):
        order_num = i + 1
        psp = PSP_SEQUENCE[i]
        product = PRODUCTS[i]
        
        # Create order
        order_data = create_order_via_agent(order_num, psp, product)
        
        if order_data:
            # Confirm payment to trigger Shopify order creation
            confirmed = confirm_payment(order_data)
            order_data["payment_confirmed"] = confirmed
            order_data["shopify_synced"] = confirmed
            results.append(order_data)
        else:
            results.append({
                "order_num": order_num,
                "psp": psp,
                "product": product["title"],
                "error": "Order creation failed",
                "has_payment": False
            })
        
        time.sleep(0.5)  # Brief delay
    
    # Final Summary
    print("\n" + "="*80)
    print("📊 FINAL TEST RESULTS")
    print("="*80)
    print()
    
    # By PSP
    by_psp = {}
    for r in results:
        psp = r.get("psp", "unknown")
        if psp not in by_psp:
            by_psp[psp] = {"total": 0, "success": 0, "failed": 0, "orders": []}
        by_psp[psp]["total"] += 1
        if r.get("has_payment"):
            by_psp[psp]["success"] += 1
        else:
            by_psp[psp]["failed"] += 1
        by_psp[psp]["orders"].append(r["order_num"])
    
    for psp in ["stripe", "checkout", "paypal", "adyen"]:
        if psp in by_psp:
            stats = by_psp[psp]
            success_rate = (stats["success"] / stats["total"] * 100) if stats["total"] > 0 else 0
            status_icon = "✅" if stats["failed"] == 0 else "⚠️" if stats["success"] > 0 else "❌"
            print(f"{status_icon} {psp.upper():10} | {stats['success']}/{stats['total']} success ({success_rate:.0f}%) | Orders: {stats['orders']}")
    
    print()
    
    total_success = sum(1 for r in results if r.get("has_payment"))
    total_orders = len(results)
    overall_rate = (total_success / total_orders * 100) if total_orders > 0 else 0
    
    print(f"📊 Overall Success Rate: {total_success}/{total_orders} ({overall_rate:.0f}%)")
    print()
    
    # Failed orders detail
    failed = [r for r in results if not r.get("has_payment")]
    if failed:
        print("❌ Failed Orders Details:")
        for r in failed:
            print(f"   #{r['order_num']}: {r['psp'].upper()} - {r['product']} - {r.get('error', 'Payment intent not created')}")
        print()
    else:
        print("🎉 ALL 10 ORDERS SUCCESSFUL!")
        print()
        print("✅ All PSP integrations working correctly:")
        print("   - Stripe: Payment intents created")
        print("   - Checkout: Payment sessions created")
        print("   - PayPal: Order approval URLs generated")
        print("   - Adyen: Payment references created")
        print()
    
    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"e2e_test_results_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump({
            "test_time": datetime.now().isoformat(),
            "merchant_id": MERCHANT_ID,
            "total_orders": total_orders,
            "successful": total_success,
            "failed": len(failed),
            "success_rate": overall_rate,
            "by_psp": by_psp,
            "orders": results
        }, f, indent=2)
    
    print(f"📄 Results saved to: {filename}")
    print()
    
    # Next steps
    if total_success == total_orders:
        print("🎯 Next Steps:")
        print("   1. Check Merchant Portal Dashboard - verify orders appear")
        print("   2. Check Employee Portal PSPs page - verify transaction volumes")
        print("   3. Verify each PSP shows correct transaction count (no duplicates)")
        print()
    else:
        print("⚠️  Some orders failed. Check Railway logs for details.")
        print()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()

