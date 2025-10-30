#!/usr/bin/env python3
import httpx
import asyncio
import random
from datetime import datetime, timedelta

# Configuration
API_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"

# Test data
CUSTOMERS = [
    {"name": "John Smith", "email": "john.smith@example.com"},
    {"name": "Jane Doe", "email": "jane.doe@example.com"},
    {"name": "Bob Johnson", "email": "bob.johnson@example.com"},
    {"name": "Alice Brown", "email": "alice.brown@example.com"},
    {"name": "Charlie Wilson", "email": "charlie.wilson@example.com"}
]

PRODUCTS = [
    {"id": "LAPTOP-001", "name": "Premium Laptop", "price": 1299.99},
    {"id": "MOUSE-002", "name": "Wireless Mouse", "price": 49.99},
    {"id": "KEYBOARD-003", "name": "Mechanical Keyboard", "price": 159.99},
    {"id": "MONITOR-004", "name": "4K Monitor", "price": 599.99},
    {"id": "HEADSET-005", "name": "Gaming Headset", "price": 89.99}
]

PSPS = ["stripe", "adyen", "paypal", "checkout"]

async def create_order_via_api(order_data):
    """Create order using the order routes API"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{API_URL}/api/orders/create",
                json=order_data
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"✅ Created order: {result['order_id']} - Total: ${order_data['items'][0]['price']}")
                return result
            else:
                print(f"❌ Failed to create order: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"❌ Error creating order: {e}")
            return None

async def generate_test_orders():
    """Generate 5 test orders"""
    print(f"\n🚀 Generating 5 test orders for merchant {MERCHANT_ID}...\n")
    
    orders_created = 0
    psp_distribution = {}
    
    for i, customer in enumerate(CUSTOMERS):
        # Random product selection
        product = random.choice(PRODUCTS)
        
        # Random PSP
        psp = random.choice(PSPS)
        psp_distribution[psp] = psp_distribution.get(psp, 0) + 1
        
        # Random date in last 30 days
        days_ago = random.randint(0, 30)
        order_date = datetime.now() - timedelta(days=days_ago)
        
        # Create order data
        order_data = {
            "merchant_id": MERCHANT_ID,
            "customer_email": customer["email"],
            "items": [{
                "product_id": product["id"],
                "name": product["name"],
                "quantity": 1,
                "price": product["price"]
            }],
            "shipping_address": {
                "name": customer["name"],
                "street": f"{random.randint(100, 999)} Main Street",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US"
            },
            "currency": "USD",
            "preferred_psp": psp,
            "metadata": {
                "test_order": True,
                "created_at": order_date.isoformat()
            }
        }
        
        # Create order
        result = await create_order_via_api(order_data)
        if result:
            orders_created += 1
        
        # Small delay between orders
        await asyncio.sleep(1)
    
    print(f"\n✨ Summary: {orders_created}/5 orders created successfully")
    
    print("\n📊 PSP Distribution:")
    for psp, count in psp_distribution.items():
        print(f"  - {psp}: {count} orders")

if __name__ == "__main__":
    asyncio.run(generate_test_orders())

