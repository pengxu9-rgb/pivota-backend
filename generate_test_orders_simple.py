"""
Simple test order generator with hardcoded API key
"""
import requests
import time
import random

# Use the verified working API key
API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
API_BASE = "https://web-production-fedb.up.railway.app/agent/v1"
MERCHANT_ID = "merch_6b90dc9838d5fd9c"

headers = {"x-api-key": API_KEY}

print("🚀 Testing API key first...")
merchants_resp = requests.get(f"{API_BASE}/merchants", headers=headers)
print(f"Merchants endpoint: {merchants_resp.status_code}")

if merchants_resp.status_code != 200:
    print(f"❌ API key not working: {merchants_resp.text}")
    exit(1)

print("✅ API key works!")
print("\n🔍 Searching for products...")

products_resp = requests.get(
    f"{API_BASE}/products/search",
    headers=headers,
    params={"query": "coffee", "limit": 20}  # Search across all merchants
)

print(f"Products search: {products_resp.status_code}")
if products_resp.status_code == 200:
    products = products_resp.json().get("products", [])
    print(f"✅ Found {len(products)} products")
    
    if products:
        print("\nCreating 120 orders...")
        success = 0
        failed = 0
        
        for i in range(120):
            product = random.choice(products)
            # Use the merchant_id from the product
            merchant_id = product.get("merchant_id", MERCHANT_ID)
            
            order_data = {
                "merchant_id": merchant_id,
                "items": [{
                    "product_id": product.get("id") or product.get("product_id"),
                    "product_title": product.get("name", "Product"),
                    "quantity": random.randint(1, 3),
                    "unit_price": float(product.get("price", 0)),
                    "subtotal": float(product.get("price", 0)) * random.randint(1, 3)
                }],
                "customer_email": f"customer{i}@test.com",
                "shipping_address": {
                    "name": f"Customer {i}",
                    "address_line1": "123 Main St",
                    "city": "San Francisco",
                    "state": "CA",
                    "postal_code": "94102",
                    "country": "US"
                }
            }
            
            order_resp = requests.post(
                f"{API_BASE}/orders/create",
                headers=headers,
                json=order_data
            )
            
            if order_resp.status_code in [200, 201]:
                order = order_resp.json()
                success += 1
                print(f"   [{i+1}/120] ✅ {order.get('order_id')}: ${order.get('total', 0)}")
            else:
                failed += 1
                print(f"   [{i+1}/120] ❌ {order_resp.status_code}: {order_resp.text[:100]}")
            
            time.sleep(0.5)
        
        print(f"\n✅ Success: {success}, ❌ Failed: {failed}")
    else:
        print("❌ No products found")
else:
    print(f"❌ Product search failed: {products_resp.text}")

