#!/usr/bin/env python3
import httpx
import asyncio
import random
from datetime import datetime, timedelta
import json

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

CUSTOMERS = [
    {"name": "John Smith", "email": "john.smith@example.com"},
    {"name": "Jane Doe", "email": "jane.doe@example.com"},
    {"name": "Bob Johnson", "email": "bob.johnson@example.com"},
    {"name": "Alice Williams", "email": "alice.williams@example.com"},
    {"name": "Charlie Brown", "email": "charlie.brown@example.com"}
]

PSPS = ["stripe", "adyen", "paypal", "checkout"]
PAYMENT_STATUSES = ["paid", "paid", "paid", "pending", "failed"]  # 60% success rate

async def create_order_via_api(order_data):
    """Create order via order routes API"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{API_BASE_URL}/api/orders/create",
                json=order_data
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"✅ Created order: {result['order_id']} - ${result['total']:.2f}")
                return result
            else:
                print(f"❌ Failed to create order: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"❌ Error creating order: {e}")
            return None

async def generate_orders():
    """Generate 5 test orders"""
    print(f"\n🚀 Generating 5 test orders for merchant {MERCHANT_ID}...\n")
    
    tasks = []
    
    for i in range(5):
        # Random customer
        customer = random.choice(CUSTOMERS)
        
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
        
        # Random PSP
        psp = random.choice(PSPS)
        
        # Order data
        order_data = {
            "merchant_id": MERCHANT_ID,
            "customer_email": customer["email"],
            "items": items,
            "shipping_address": {
                "street": f"{random.randint(100, 999)} Main Street",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US"
            },
            "currency": "USD",
            "preferred_psp": psp
        }
        
        tasks.append(create_order_via_api(order_data))
    
    # Execute all orders
    results = await asyncio.gather(*tasks)
    
    # Summary
    successful = sum(1 for r in results if r is not None)
    print(f"\n✨ Summary: {successful}/5 orders created successfully")
    
    # Show created orders
    if successful > 0:
        print("\n📋 Created Orders:")
        for result in results:
            if result:
                print(f"  - Order ID: {result['order_id']}")
                print(f"    Customer: {result.get('customer_email', 'N/A')}")
                print(f"    Total: ${result['total']:.2f}")
                print(f"    Status: {result.get('status', 'created')}")

if __name__ == "__main__":
    asyncio.run(generate_orders())


