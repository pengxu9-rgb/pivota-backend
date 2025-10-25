"""
Generate 50 test orders that will use Checkout.com for payment
"""
import requests
import time
import random

API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
API_BASE = "https://web-production-fedb.up.railway.app/agent/v1"

# Use the merchant that has Checkout configured
MERCHANT_ID = "merch_208139f7600dbf42"  # chydantest - has Checkout connected (products synced)

headers = {"x-api-key": API_KEY}

print("🚀 Generating 50 orders with Checkout.com payment")
print("=" * 60)
print(f"Merchant: {MERCHANT_ID}")
print(f"Payment Provider: Checkout.com (will fallback from Stripe)")
print("=" * 60)

# Search for products from this merchant
print("\n📦 Searching for products...")
products_resp = requests.get(
    f"{API_BASE}/products/search",
    headers=headers,
    params={"query": "coffee", "limit": 10}  # Search all merchants, we'll use MERCHANT_ID in order
)

if products_resp.status_code != 200:
    print(f"❌ Product search failed: {products_resp.status_code}")
    print(f"Response: {products_resp.text[:200]}")
    exit(1)

products = products_resp.json().get("products", [])
print(f"✅ Found {len(products)} products")

if not products:
    print("❌ No products found for this merchant")
    print("Note: This merchant needs to have products synced")
    exit(1)

product = products[0]
print(f"📦 Using: {product.get('name')} - ${product.get('price')}")
print("\n" + "=" * 60)
print("Creating 50 orders...\n")

success = 0
failed = 0
total_gmv = 0
checkout_used = 0

for i in range(50):
    quantity = random.randint(1, 3)
    
    order_data = {
        "merchant_id": MERCHANT_ID,
        "items": [{
            "product_id": product.get("id"),
            "product_title": product.get("name", "Product"),
            "quantity": quantity,
            "unit_price": float(product.get("price", 0)),
            "subtotal": float(product.get("price", 0)) * quantity
        }],
        "customer_email": f"checkout_test_{i+1}@example.com",
        "shipping_address": {
            "name": f"Checkout Customer {i+1}",
            "address_line1": f"{300+i} Checkout St",
            "city": "London",
            "state": "GB",
            "postal_code": "SW1A 1AA",
            "country": "GB"
        }
    }
    
    try:
        order_resp = requests.post(
            f"{API_BASE}/orders/create",
            headers=headers,
            json=order_data,
            timeout=30
        )
        
        if order_resp.status_code in [200, 201]:
            order = order_resp.json()
            success += 1
            order_total = float(order.get('total', 0))
            total_gmv += order_total
            
            # Check which PSP was used (if available in response)
            payment_info = order.get('payment', {})
            psp_used = "Unknown"
            if 'checkout' in str(payment_info).lower():
                checkout_used += 1
                psp_used = "Checkout"
            elif 'stripe' in str(payment_info).lower() or 'pi_' in str(payment_info.get('payment_intent_id', '')):
                psp_used = "Stripe"
            
            if (i + 1) % 10 == 0:
                print(f"   [{i+1}/50] ✅ {success} successful | ${total_gmv:.2f} GMV | {checkout_used} via Checkout")
        else:
            failed += 1
            if (i + 1) % 10 == 0:
                print(f"   [{i+1}/50] ❌ {failed} failed")
    except Exception as e:
        failed += 1
        if (i + 1) % 10 == 0:
            print(f"   [{i+1}/50] ⚠️  Error: {str(e)[:50]}")
    
    # Small delay
    if (i + 1) % 20 == 0:
        time.sleep(1)

print("\n" + "=" * 60)
print("📊 FINAL RESULTS")
print("=" * 60)
print(f"✅ Successful orders: {success}/50")
print(f"❌ Failed orders: {failed}/50")
print(f"💵 Total GMV: ${total_gmv:.2f}")
print(f"📈 Success rate: {(success/50)*100:.1f}%")
print(f"🏦 Checkout.com used: {checkout_used} times")
print("=" * 60)

if success > 0:
    print("\n🎉 Orders created! Check:")
    print("   • Agent Portal Dashboard: https://agents.pivota.cc/dashboard")
    print("   • Employee PSPs Page: Check Checkout.com transaction count")
    print(f"   • Expected: Checkout should show ~{success} total transactions")

