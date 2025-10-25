"""
Generate 60 test orders for dashboard verification
"""
import requests
import time
import random

API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
API_BASE = "https://web-production-fedb.up.railway.app/agent/v1"

headers = {"x-api-key": API_KEY}

print("🚀 Generating 60 test orders...")
print("=" * 60)

# Search for products
print("📦 Searching for products...")
products_resp = requests.get(
    f"{API_BASE}/products/search",
    headers=headers,
    params={"query": "coffee", "limit": 10}
)

if products_resp.status_code != 200:
    print(f"❌ Product search failed: {products_resp.status_code}")
    exit(1)

products = products_resp.json().get("products", [])
print(f"✅ Found {len(products)} products")

if not products:
    print("❌ No products found")
    exit(1)

product = products[0]
print(f"📦 Using: {product.get('name')} - ${product.get('price')}")
print(f"🏪 Merchant: {product.get('merchant_id')}")
print("=" * 60)
print("\nCreating 60 orders...\n")

success = 0
failed = 0
total_gmv = 0

for i in range(60):
    quantity = random.randint(1, 3)
    
    order_data = {
        "merchant_id": product.get("merchant_id"),
        "items": [{
            "product_id": product.get("id"),
            "product_title": product.get("name", "Product"),
            "quantity": quantity,
            "unit_price": float(product.get("price", 0)),
            "subtotal": float(product.get("price", 0)) * quantity
        }],
        "customer_email": f"customer{i+1}@test.com",
        "shipping_address": {
            "name": f"Customer {i+1}",
            "address_line1": f"{200+i} Market St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94102",
            "country": "US"
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
            
            # Show progress every 10 orders
            if (i + 1) % 10 == 0:
                print(f"   [{i+1}/60] ✅ {success} successful, ${total_gmv:.2f} GMV")
        else:
            failed += 1
            if (i + 1) % 10 == 0:
                print(f"   [{i+1}/60] ❌ {failed} failed")
    except Exception as e:
        failed += 1
        if (i + 1) % 10 == 0:
            print(f"   [{i+1}/60] ❌ Error: {str(e)[:50]}")
    
    # Small delay to avoid overwhelming the API
    if (i + 1) % 20 == 0:
        time.sleep(1)

print("\n" + "=" * 60)
print("📊 FINAL RESULTS")
print("=" * 60)
print(f"✅ Successful orders: {success}/60")
print(f"❌ Failed orders: {failed}/60")
print(f"💵 Total GMV: ${total_gmv:.2f}")
print(f"📈 Success rate: {(success/60)*100:.1f}%")
print("=" * 60)

if success > 0:
    print("\n🎉 Success! Check the Agent Portal Dashboard:")
    print("   https://agents.pivota.cc/dashboard")
    print("\n📊 Expected dashboard data:")
    print(f"   • Orders Initiated: ~{success}")
    print(f"   • Payment Attempted: ~{success}")
    print(f"   • Total GMV: ${total_gmv:.2f}")
    print(f"   • Product Searches: should increase by 1")
    print(f"   • API Calls Today: should increase by ~{success + 1}")



