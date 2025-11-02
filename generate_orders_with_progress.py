"""
Generate test orders with progress indicator
"""
import requests
import time
import random
import sys

API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
API_BASE = "https://web-production-fedb.up.railway.app/agent/v1"

headers = {"x-api-key": API_KEY}

print("🚀 Generating test orders with progress...")
print("⚠️  Note: API responses are slow (3-5 seconds each)")
print("=" * 60)

# Search for products
print("📦 Fetching products... (this may take 5-10 seconds)")
sys.stdout.flush()

products_resp = requests.get(
    f"{API_BASE}/products/search",
    headers=headers,
    params={"query": "coffee", "limit": 1},
    timeout=30
)

if products_resp.status_code != 200:
    print(f"❌ Product search failed: {products_resp.status_code}")
    exit(1)

products = products_resp.json().get("products", [])
if not products:
    print("❌ No products found")
    exit(1)

product = products[0]
print(f"✅ Found product: {product.get('name')} - ${product.get('price')}")
print(f"🏪 Merchant: {product.get('merchant_id')}")
print("=" * 60)

# Generate fewer orders for demo
NUM_ORDERS = 10
print(f"\n📊 Creating {NUM_ORDERS} test orders...")
print("⏱️  Estimated time: ~1 minute\n")

success = 0
failed = 0
total_gmv = 0

for i in range(NUM_ORDERS):
    quantity = random.randint(1, 3)
    
    # Show progress
    print(f"[{i+1}/{NUM_ORDERS}] Creating order... ", end="")
    sys.stdout.flush()
    
    order_data = {
        "merchant_id": product.get("merchant_id"),
        "items": [{
            "product_id": product.get("id"),
            "product_title": product.get("name", "Product"),
            "quantity": quantity,
            "unit_price": float(product.get("price", 0)),
            "subtotal": float(product.get("price", 0)) * quantity
        }],
        "customer_email": f"demo{i+1}@test.com",
        "shipping_address": {
            "name": f"Demo Customer {i+1}",
            "address_line1": f"{100+i} Market St",
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
            print(f"✅ Success! Order: {order.get('order_id')[:12]}... Total: ${order_total:.2f}")
        else:
            failed += 1
            print(f"❌ Failed: HTTP {order_resp.status_code}")
    except requests.exceptions.Timeout:
        failed += 1
        print("❌ Timeout (>30 seconds)")
    except Exception as e:
        failed += 1
        print(f"❌ Error: {str(e)[:50]}")
    
    sys.stdout.flush()

print("\n" + "=" * 60)
print("📊 FINAL RESULTS")
print("=" * 60)
print(f"✅ Successful orders: {success}/{NUM_ORDERS}")
print(f"❌ Failed orders: {failed}/{NUM_ORDERS}")
print(f"💵 Total GMV: ${total_gmv:.2f}")
print(f"📈 Success rate: {(success/NUM_ORDERS)*100:.1f}%")
print("=" * 60)

if success > 0:
    print("\n🎉 Success! Check the Agent Portal Dashboard:")
    print("   https://agents.pivota.cc/dashboard")
    print("\n📊 The dashboard should now show:")
    print(f"   • {success} new orders in Recent Activity")
    print(f"   • ${total_gmv:.2f} in total GMV")
    print(f"   • Updated API call metrics")
    
print("\n💡 Note: The API is responding slowly (3-5 seconds per request).")
print("   This is why the scripts appear to 'hang' - they're waiting for responses.")




