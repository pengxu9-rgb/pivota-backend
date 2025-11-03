"""
Quick test - generate 5 orders to verify everything works
"""
import requests
import time
import random

API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
API_BASE = "https://web-production-fedb.up.railway.app/agent/v1"

headers = {"x-api-key": API_KEY}

print("🚀 Quick test - Creating 5 orders...")

# Search for products
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

# Create 5 orders
success = 0
failed = 0

for i in range(5):
    product = products[0]  # Use first product
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
        "customer_email": f"test{i}@example.com",
        "shipping_address": {
            "name": f"Test Customer {i}",
            "address_line1": "123 Main St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94102",
            "country": "US"
        }
    }
    
    print(f"\n[{i+1}/5] Creating order for {quantity}x {product.get('name')}...")
    
    order_resp = requests.post(
        f"{API_BASE}/orders/create",
        headers=headers,
        json=order_data
    )
    
    if order_resp.status_code in [200, 201]:
        order = order_resp.json()
        success += 1
        print(f"   ✅ Success! Order ID: {order.get('order_id')}")
        print(f"      Total: ${order.get('total', 0)}")
        print(f"      Status: {order.get('status')}")
    else:
        failed += 1
        print(f"   ❌ Failed: {order_resp.status_code}")
        print(f"      Error: {order_resp.text[:200]}")
    
    time.sleep(0.5)

print(f"\n{'='*60}")
print(f"📊 Results: ✅ {success} successful, ❌ {failed} failed")
print(f"{'='*60}")

if success > 0:
    print("\n🎉 Check the Agent Portal Dashboard:")
    print("   https://agents.pivota.cc/dashboard")
