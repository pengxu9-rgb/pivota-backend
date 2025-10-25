"""
Generate 100+ test orders using agent@test.com account
This will create real API usage data for testing the full pipeline
"""

import requests
import time
import random
from datetime import datetime, timedelta

# Configuration
API_BASE = "https://web-production-fedb.up.railway.app"
AGENT_EMAIL = "agent@test.com"
AGENT_PASSWORD = "Admin123!"
TARGET_ORDERS = 120  # Generate 120 orders for good test data

# Test data
CUSTOMER_EMAILS = [
    "customer1@test.com", "customer2@test.com", "customer3@test.com",
    "buyer@example.com", "shopper@test.com", "user@mail.com",
    "client@business.com", "john@doe.com", "jane@smith.com",
    "test@pivota.cc"
]

SHIPPING_ADDRESSES = [
    {
        "name": "John Doe",
        "line1": "123 Main St",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94102",
        "country": "US"
    },
    {
        "name": "Jane Smith",
        "line1": "456 Market St",
        "city": "New York",
        "state": "NY",
        "postal_code": "10001",
        "country": "US"
    },
    {
        "name": "Bob Johnson",
        "line1": "789 Broadway",
        "city": "Los Angeles",
        "state": "CA",
        "postal_code": "90001",
        "country": "US"
    },
]

def login_agent():
    """Login as agent@test.com and get token"""
    print("🔐 Logging in as agent@test.com...")
    response = requests.post(
        f"{API_BASE}/auth/signin",
        json={"email": AGENT_EMAIL, "password": AGENT_PASSWORD}
    )
    
    if response.status_code == 200:
        data = response.json()
        token = data.get("token")
        print(f"✅ Login successful! Token: {token[:20]}...")
        return token
    else:
        print(f"❌ Login failed: {response.status_code} - {response.text}")
        return None

def get_agent_api_key(jwt_token):
    """Get the agent's API key from their profile"""
    # First, get agent from agents table
    response = requests.post(
        f"{API_BASE}/admin/init/agent-test-key"
    )
    if response.status_code == 200:
        data = response.json()
        api_key = data.get("agent_details", {}).get("api_key")
        if api_key:
            print(f"✅ Got API key: {api_key[:15]}...")
            return api_key
    
    # Fallback: use the known good key
    print("⚠️  Using default API key")
    return "ak_live_d2b8ab4084582406a671cfa87f357325b3638003df499b3595d1254b119d03ca"

def get_merchants(api_key):
    """Get list of available merchants"""
    print("\n📋 Fetching merchants...")
    headers = {"x-api-key": api_key}
    
    response = requests.get(
        f"{API_BASE}/agent/v1/merchants",
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        merchants = data.get("merchants", [])
        print(f"✅ Found {len(merchants)} merchants")
        return merchants
    else:
        print(f"⚠️  Merchant fetch failed: {response.status_code}")
        # Return default merchant
        return [{"merchant_id": "merch_6b90dc9838d5fd9c", "business_name": "ChydanTest Store"}]

def search_products(api_key, merchant_id):
    """Search for products"""
    headers = {"x-api-key": api_key}
    
    queries = ["coffee", "mug", "shirt", "laptop", "phone", "book", "pen", "bag"]
    query = random.choice(queries)
    
    response = requests.get(
        f"{API_BASE}/agent/v1/products/search",
        headers=headers,
        params={"query": query, "merchant_id": merchant_id, "limit": 10}
    )
    
    if response.status_code == 200:
        data = response.json()
        products = data.get("products", [])
        return products
    else:
        print(f"   ⚠️  Product search failed for '{query}': {response.status_code}")
        return []

def create_order(api_key, merchant_id, product, customer_email, shipping_address):
    """Create an order"""
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    quantity = random.randint(1, 3)
    
    order_data = {
        "merchant_id": merchant_id,
        "items": [{
            "product_id": product.get("id") or product.get("product_id"),
            "quantity": quantity,
            "price": float(product.get("price", 0))
        }],
        "customer_email": customer_email,
        "shipping_address": shipping_address
    }
    
    response = requests.post(
        f"{API_BASE}/agent/v1/orders/create",
        headers=headers,
        json=order_data
    )
    
    return response

def generate_orders(api_key, target_count=100):
    """Generate multiple test orders"""
    print(f"\n🚀 Starting to generate {target_count} test orders...\n")
    
    # Get merchants
    merchants = get_merchants(api_key)
    if not merchants:
        print("❌ No merchants available")
        return
    
    merchant = merchants[0]
    merchant_id = merchant["merchant_id"]
    print(f"📍 Using merchant: {merchant.get('business_name')} ({merchant_id})\n")
    
    success_count = 0
    fail_count = 0
    
    for i in range(target_count):
        try:
            # Search for products
            products = search_products(api_key, merchant_id)
            
            if not products:
                print(f"   [{i+1}/{target_count}] ⚠️  No products found, skipping...")
                fail_count += 1
                time.sleep(0.5)
                continue
            
            # Pick a random product
            product = random.choice(products)
            
            # Pick random customer and address
            customer_email = random.choice(CUSTOMER_EMAILS)
            shipping_address = random.choice(SHIPPING_ADDRESSES)
            
            # Create order
            response = create_order(api_key, merchant_id, product, customer_email, shipping_address)
            
            if response.status_code in [200, 201]:
                order = response.json()
                order_id = order.get("order_id", "unknown")
                total = order.get("total", 0)
                success_count += 1
                print(f"   [{i+1}/{target_count}] ✅ Order {order_id}: ${total:.2f} - {product.get('name', 'Product')[:30]}")
            else:
                fail_count += 1
                print(f"   [{i+1}/{target_count}] ❌ Failed: {response.status_code} - {response.text[:100]}")
            
            # Rate limiting: wait a bit between orders
            time.sleep(random.uniform(0.3, 0.8))
            
        except Exception as e:
            fail_count += 1
            print(f"   [{i+1}/{target_count}] ❌ Exception: {str(e)[:100]}")
            time.sleep(1)
    
    # Summary
    print("\n" + "="*60)
    print(f"📊 Order Generation Complete!")
    print(f"✅ Success: {success_count}")
    print(f"❌ Failed: {fail_count}")
    print(f"📈 Success Rate: {(success_count/target_count*100):.1f}%")
    print("="*60)

def main():
    print("="*60)
    print("🎯 Pivota Agent - Test Order Generator")
    print("="*60)
    
    # Login to get JWT token
    jwt_token = login_agent()
    if not jwt_token:
        print("❌ Cannot proceed without authentication")
        return
    
    # Get API key for agent API calls
    api_key = get_agent_api_key(jwt_token)
    
    # Generate orders
    generate_orders(api_key, TARGET_ORDERS)
    
    print(f"\n✅ All done! Check the Agent Portal Dashboard for updated metrics.")
    print(f"   Dashboard: https://agents.pivota.cc/dashboard")

if __name__ == "__main__":
    main()

