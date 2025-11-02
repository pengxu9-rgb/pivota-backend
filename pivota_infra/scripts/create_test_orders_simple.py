#!/usr/bin/env python3
import httpx
import asyncio
import random
from datetime import datetime

# Configuration
API_BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"

# Sample data
PRODUCTS = [
    {"id": "prod_001", "name": "Premium Laptop", "price": 1299.99},
    {"id": "prod_002", "name": "Wireless Mouse", "price": 49.99},
    {"id": "prod_003", "name": "USB-C Hub", "price": 79.99},
    {"id": "prod_004", "name": "Monitor Stand", "price": 129.99},
    {"id": "prod_005", "name": "Mechanical Keyboard", "price": 159.99}
]

async def create_test_orders():
    """Create 5 test orders via the orders/create endpoint"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"\n🚀 Creating 5 test orders for merchant {MERCHANT_ID}...\n")
        
        created_count = 0
        
        for i in range(5):
            # Random products (1-3)
            num_products = random.randint(1, 3)
            selected_products = random.sample(PRODUCTS, num_products)
            
            # Build order items
            items = []
            for product in selected_products:
                items.append({
                    "product_id": product["id"],
                    "name": product["name"],
                    "quantity": random.randint(1, 2),
                    "price": product["price"]
                })
            
            # Order data
            order_data = {
                "merchant_id": MERCHANT_ID,
                "customer_email": f"customer{i+1}@example.com",
                "items": items,
                "shipping_address": {
                    "street": f"{100+i} Main Street",
                    "city": "San Francisco",
                    "state": "CA",
                    "postal_code": "94105",
                    "country": "US"
                },
                "currency": "USD",
                "preferred_psp": random.choice(["stripe", "adyen", "paypal", "checkout"])
            }
            
            try:
                print(f"Creating order {i+1}...")
                response = await client.post(
                    f"{API_BASE_URL}/orders/create",
                    json=order_data
                )
                
                if response.status_code == 200:
                    result = response.json()
                    print(f"✅ Order {i+1}: {result['order_id']} - ${result['total']:.2f}")
                    created_count += 1
                else:
                    print(f"❌ Order {i+1} failed: {response.status_code} - {response.text}")
                    
            except Exception as e:
                print(f"❌ Order {i+1} error: {e}")
        
        print(f"\n✨ Summary: {created_count}/5 orders created successfully")
        
        if created_count > 0:
            print("\n📊 Next steps:")
            print("1. Go to Merchant Portal > Analytics")
            print("2. Check if the orders appear in the charts")
            print("3. Verify PSP distribution and revenue data")

if __name__ == "__main__":
    asyncio.run(create_test_orders())


